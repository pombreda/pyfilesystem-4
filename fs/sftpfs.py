"""
fs.sftpfs
=========

Filesystem accessing an SFTP server (via paramiko)

"""

import datetime
import stat as statinfo
import threading
import os
import paramiko
from getpass import getuser
from binascii import hexlify

from fs.base import *
from fs.path import *
from fs.errors import *
from fs.utils import isdir, isfile

# SFTPClient appears to not be thread-safe, so we use an instance per thread
if hasattr(threading, "local"):
    thread_local = threading.local
else:
    class thread_local(object):
        def __init__(self):
            self._map = {}
        def __getattr__(self,attr):
            try:
                return self._map[(threading.currentThread().ident,attr)]
            except KeyError:
                raise AttributeError, attr
        def __setattr__(self,attr,value):
            self._map[(threading.currentThread().ident,attr)] = value



if not hasattr(paramiko.SFTPFile,"__enter__"):
    paramiko.SFTPFile.__enter__ = lambda self: self
    paramiko.SFTPFile.__exit__ = lambda self,et,ev,tb: self.close() and False


class SFTPFS(FS):
    """A filesystem stored on a remote SFTP server.

    This is basically a compatability wrapper for the excellent SFTPClient
    class in the paramiko module.
    
    """

    _meta = { 'virtual': False,
              'read_only' : False,
              'unicode_paths' : True,
              'case_insensitive_paths' : False,
              'network' : True,
              'atomic.move' : True,
              'atomic.copy' : True,
              'atomic.makedir' : True,
              'atomic.rename' : True,
              'atomic.setcontents' : False
              }


    def __init__(self, connection, root_path="/", encoding=None, username='', password=None, pkey=None):
        """SFTPFS constructor.

        The only required argument is 'connection', which must be something
        from which we can construct a paramiko.SFTPClient object.  Possibile
        values include:

            * a hostname string
            * a (hostname,port) tuple
            * a paramiko.Transport instance
            * a paramiko.Channel instance in "sftp" mode

        The kwd argument 'root_path' specifies the root directory on the remote
        machine - access to files outsite this root wil be prevented. 
        
        :param connection: a connection string
        :param root_path: The root path to open
        :param encoding: String encoding of paths (defaults to UTF-8)
        :param username: Name of SFTP user
        :param password: Password for SFTP user
        :param pkey: Public key
        
        """
        
        credentials = dict(username=username,
                           password=password,
                           pkey=pkey)
        
        if encoding is None:
            encoding = "utf8"
        self.encoding = encoding
        self.closed = False
        self._owns_transport = False
        self._credentials = credentials
        self._tlocal = thread_local()
        self._transport = None
        self._client = None
        
                    
        super(SFTPFS, self).__init__()        
        self.root_path = abspath(normpath(root_path))
        
        if isinstance(connection,paramiko.Channel):
            self._transport = None
            self._client = paramiko.SFTPClient(connection)
        else:
            if not isinstance(connection,paramiko.Transport):
                connection = paramiko.Transport(connection)
                connection.daemon = True
                self._owns_transport = True
            
        if not connection.is_authenticated():
            
            try:                                
                connection.start_client()                    
                
                if pkey:                    
                    connection.auth_publickey(username, pkey)
                
                if not connection.is_authenticated() and password:                    
                    connection.auth_password(username, password)                    
                  
                if not connection.is_authenticated():  
                    self._agent_auth(connection, username)
                
                if not connection.is_authenticated():
                    connection.close()
                    raise RemoteConnectionError('no auth')
                
            except paramiko.SSHException, e:
                connection.close()
                raise RemoteConnectionError('SSH exception (%s)' % str(e), details=e)
                
        self._transport = connection
        

    @classmethod
    def _agent_auth(cls, transport, username):
        """
        Attempt to authenticate to the given transport using any of the private
        keys available from an SSH agent.
        """
        
        agent = paramiko.Agent()
        agent_keys = agent.get_keys()
        if len(agent_keys) == 0:
            return False
            
        for key in agent_keys:            
            try:
                transport.auth_publickey(username, key)                
                return key
            except paramiko.SSHException:
                pass
        return None       

    def __del__(self):
        self.close()

    def __getstate__(self):
        state = super(SFTPFS,self).__getstate__()
        del state["_tlocal"]
        if self._owns_transport:
            state['_transport'] = self._transport.getpeername()
        return state

    def __setstate__(self,state):
        for (k,v) in state.iteritems():
            self.__dict__[k] = v
        self._tlocal = thread_local()
        if self._owns_transport:
            self._transport = paramiko.Transport(self._transport)
            self._transport.connect(**self._credentials)

    @property
    def client(self):
        try:
            return self._tlocal.client
        except AttributeError:
            if self._transport is None:
                return self._client
            client = paramiko.SFTPClient.from_transport(self._transport)
            self._tlocal.client = client
            return client

    def close(self):
        """Close the connection to the remote server."""
        if not self.closed:
            if self.client:
                self.client.close()
            if self._owns_transport and self._transport:
                self._transport.close()
            self.closed = True

    def _normpath(self,path):
        if not isinstance(path,unicode):
            path = path.decode(self.encoding)
        npath = pathjoin(self.root_path,relpath(normpath(path)))
        if not isprefix(self.root_path,npath):
            raise PathError(path,msg="Path is outside root: %(path)s")
        return npath

    @convert_os_errors
    def open(self,path,mode="rb",bufsize=-1):
        npath = self._normpath(path)
        if self.isdir(path):
            msg = "that's a directory: %(path)s"
            raise ResourceInvalidError(path,msg=msg)
        #  paramiko implements its own buffering and write-back logic,
        #  so we don't need to use a RemoteFileBuffer here.
        f = self.client.open(npath,mode,bufsize)
        #  Unfortunately it has a broken truncate() method.
        #  TODO: implement this as a wrapper
        old_truncate = f.truncate
        def new_truncate(size=None):
            if size is None:
                size = f.tell()
            return old_truncate(size)
        f.truncate = new_truncate
        return f

    def desc(self, path):
        npath = self._normpath(path)
        addr, port = self._transport.getpeername()
        return u'%s on sftp://%s:%i' % (self.client.normalize(npath), addr, port)

    @convert_os_errors
    def exists(self,path):
        if path in ('', '/'):
            return True
        npath = self._normpath(path)
        try:
            self.client.stat(npath)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                return False
            raise
        return True

    @convert_os_errors
    def isdir(self,path):
        if path in ('', '/'):
            return True
        npath = self._normpath(path)
        try:
            stat = self.client.stat(npath)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                return False
            raise
        return statinfo.S_ISDIR(stat.st_mode)

    @convert_os_errors
    def isfile(self,path):
        npath = self._normpath(path)
        try:
            stat = self.client.stat(npath)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                return False
            raise
        return statinfo.S_ISREG(stat.st_mode)

    @convert_os_errors
    def listdir(self,path="./",wildcard=None,full=False,absolute=False,dirs_only=False,files_only=False):
        npath = self._normpath(path)
        try:            
            attrs_map = None
            if dirs_only or files_only:                
                attrs = self.client.listdir_attr(npath)
                attrs_map = dict((a.filename, a) for a in attrs)
                paths = attrs_map.keys()
            else:
                paths = self.client.listdir(npath)                
                
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                if self.isfile(path):
                    raise ResourceInvalidError(path,msg="Can't list directory contents of a file: %(path)s")
                raise ResourceNotFoundError(path)
            elif self.isfile(path):
                raise ResourceInvalidError(path,msg="Can't list directory contents of a file: %(path)s")
            raise
        
        if attrs_map:
            if dirs_only:
                filter_paths = []
                for path, attr in attrs_map.iteritems():
                    if isdir(self, path, attr.__dict__):
                        filter_paths.append(path)
                paths = filter_paths
            elif files_only:
                filter_paths = []
                for path, attr in attrs_map.iteritems():
                    if isfile(self, path, attr.__dict__):
                        filter_paths.append(path)
                paths = filter_paths
        
        for (i,p) in enumerate(paths):
            if not isinstance(p,unicode):
                paths[i] = p.decode(self.encoding)
        return self._listdir_helper(path, paths, wildcard, full, absolute, False, False)


    @convert_os_errors
    def listdirinfo(self,path="./",wildcard=None,full=False,absolute=False,dirs_only=False,files_only=False):
        npath = self._normpath(path)
        try:            
            attrs = self.client.listdir_attr(npath)
            attrs_map = dict((a.filename, a) for a in attrs)                        
            paths = attrs.keys()            
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                if self.isfile(path):
                    raise ResourceInvalidError(path,msg="Can't list directory contents of a file: %(path)s")
                raise ResourceNotFoundError(path)
            elif self.isfile(path):
                raise ResourceInvalidError(path,msg="Can't list directory contents of a file: %(path)s")
            raise
            
        if dirs_only:
            filter_paths = []
            for path, attr in attrs_map.iteritems():
                if isdir(self, path, attr.__dict__):
                    filter_paths.append(path)
            paths = filter_paths
        elif files_only:
            filter_paths = []
            for path, attr in attrs_map.iteritems():
                if isfile(self, path, attr.__dict__):
                    filter_paths.append(path)
            paths = filter_paths
            
        for (i, p) in enumerate(paths):
            if not isinstance(p, unicode):
                paths[i] = p.decode(self.encoding)
                
        def getinfo(p):
            resourcename = basename(p)
            info = attrs_map.get(resourcename)
            if info is None:
                return self.getinfo(pathjoin(path, p))
            return self._extract_info(info.__dict__)
                
        return [(p, getinfo(p)) for p in 
                    self._listdir_helper(path, paths, wildcard, full, absolute, False, False)]



    @convert_os_errors
    def makedir(self,path,recursive=False,allow_recreate=False):
        npath = self._normpath(path)
        try:
            self.client.mkdir(npath)
        except IOError, e:
            # Error code is unreliable, try to figure out what went wrong
            try:
                stat = self.client.stat(npath)
            except IOError:
                if not self.isdir(dirname(path)):
                    # Parent dir is missing
                    if not recursive:
                        raise ParentDirectoryMissingError(path)
                    self.makedir(dirname(path),recursive=True)
                    self.makedir(path,allow_recreate=allow_recreate)
                else:
                    # Undetermined error, let the decorator handle it
                    raise
            else:
                # Destination exists
                if statinfo.S_ISDIR(stat.st_mode):
                    if not allow_recreate:
                        raise DestinationExistsError(path,msg="Can't create a directory that already exists (try allow_recreate=True): %(path)s")
                else:
                    raise ResourceInvalidError(path,msg="Can't create directory, there's already a file of that name: %(path)s")

    @convert_os_errors
    def remove(self,path):
        npath = self._normpath(path)
        try:
            self.client.remove(npath)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                raise ResourceNotFoundError(path)
            elif self.isdir(path):
                raise ResourceInvalidError(path,msg="Cannot use remove() on a directory: %(path)s")
            raise

    @convert_os_errors
    def removedir(self,path,recursive=False,force=False):
        npath = self._normpath(path)
        if path in ("","/"):
            return
        if force:
            for path2 in self.listdir(path,absolute=True):
                try:
                    self.remove(path2)
                except ResourceInvalidError:
                    self.removedir(path2,force=True)
        try:
            self.client.rmdir(npath)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                if self.isfile(path):
                    raise ResourceInvalidError(path,msg="Can't use removedir() on a file: %(path)s")
                raise ResourceNotFoundError(path)
            elif self.listdir(path):
                raise DirectoryNotEmptyError(path)
            raise
        if recursive:
            try:
                self.removedir(dirname(path),recursive=True)
            except DirectoryNotEmptyError:
                pass

    @convert_os_errors
    def rename(self,src,dst):
        nsrc = self._normpath(src)
        ndst = self._normpath(dst)
        try:
            self.client.rename(nsrc,ndst)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                raise ResourceNotFoundError(path)
            if not self.isdir(dirname(dst)):
                raise ParentDirectoryMissingError(dst)
            raise

    @convert_os_errors
    def move(self,src,dst,overwrite=False,chunk_size=16384):
        nsrc = self._normpath(src)
        ndst = self._normpath(dst)
        if overwrite and self.isfile(dst):
            self.remove(dst)
        try:
            self.client.rename(nsrc,ndst)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                raise ResourceNotFoundError(path)
            if self.exists(dst):
                raise DestinationExistsError(dst)
            if not self.isdir(dirname(dst)):
                raise ParentDirectoryMissingError(dst,msg="Destination directory does not exist: %(path)s")
            raise

    @convert_os_errors
    def movedir(self,src,dst,overwrite=False,ignore_errors=False,chunk_size=16384):
        nsrc = self._normpath(src)
        ndst = self._normpath(dst)
        if overwrite and self.isdir(dst):
            self.removedir(dst)
        try:
            self.client.rename(nsrc,ndst)
        except IOError, e:
            if getattr(e,"errno",None) == 2:
                raise ResourceNotFoundError(src)
            if self.exists(dst):
                raise DestinationExistsError(dst)
            if not self.isdir(dirname(dst)):
                raise ParentDirectoryMissingError(dst,msg="Destination directory does not exist: %(path)s")
            raise

    _info_vars = frozenset('st_size st_uid st_gid st_mode st_atime st_mtime'.split())
    @classmethod
    def _extract_info(cls, stats):
        fromtimestamp = datetime.datetime.fromtimestamp
        info = dict((k, v) for k, v in stats.iteritems() if k in cls._info_vars)        
        info['size'] = info['st_size']
        ct = info.get('st_ctime')
        if ct is not None:
            info['created_time'] = fromtimestamp(ct)
        at = info.get('st_atime')
        if at is not None:
            info['accessed_time'] = fromtimestamp(at)
        mt = info.get('st_mtime')
        if mt is not None:
            info['modified_time'] = fromtimestamp(mt)
        return info

    @convert_os_errors
    def getinfo(self, path):        
        npath = self._normpath(path)
        stats = self.client.stat(npath)
        info = dict((k, getattr(stats, k)) for k in dir(stats) if not k.startswith('__') )        
        info['size'] = info['st_size']
        ct = info.get('st_ctime', None)
        if ct is not None:
            info['created_time'] = datetime.datetime.fromtimestamp(ct)
        at = info.get('st_atime', None)
        if at is not None:
            info['accessed_time'] = datetime.datetime.fromtimestamp(at)
        mt = info.get('st_mtime', None)
        if mt is not None:
            info['modified_time'] = datetime.datetime.fromtimestamp(mt)
        return info

    @convert_os_errors
    def getsize(self, path):
        npath = self._normpath(path)
        stats = self.client.stat(npath)
        return stats.st_size
