# Copyright (C) 2018  Abhinav Saxena <xandfury@gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

from os import R_OK, W_OK
from lxml import etree
import conpot.core as conpot_core
from gevent.server import StreamServer
from conpot.protocols.ftp.ftp_utils import ftp_commands, FTPException
from conpot.protocols.ftp.ftp_handler import FTPCommandChannel
from conpot.core.protocol_wrapper import conpot_protocol

# import logging
# logger = logging.getLogger(__name__)
import sys
import logging as logger
logger.basicConfig(stream=sys.stdout, level=logger.INFO)


class FTPConfig(object):
    def __init__(self, template):
        dom = etree.parse(template)
        self.device_type = dom.xpath('//ftp/device_info/device_type/text()')[0]
        self.banner = dom.xpath('//ftp/device_info/banner/text()')[0]
        self.max_login_attempts = int(dom.xpath('//ftp/device_info/max_login_attempts/text()')[0])
        self.anon_auth = bool(dom.xpath('//ftp/anon_login/text()')[0])
        # Implementation Note: removing a command from here would make it unrecognizable in FTP server.
        self.enabled_commands = ['USER', 'PASS', 'HELP', 'NOOP', 'QUIT', 'SITE HELP', 'SITE', 'SYST', 'TYPE', 'PASV',
                                 'PORT', 'ALLO', 'MODE', 'SIZE', 'PWD', 'MKD', 'RMD', 'CWD', 'CDUP', 'MDTM', 'DELE',
                                 'SITE CHMOD', 'RNFR', 'RNTO', 'STAT', 'LIST', 'NLST', 'RETR', 'REIN', 'ABOR', 'STOR']
        # Restrict FTP to only enabled FTP commands
        self.COMMANDS = {i: ftp_commands[i] for i in self.enabled_commands}
        # VFS related.
        self.root_path = dom.xpath('//ftp/vfs/path/text()')[0]
        self.data_fs_subdir = dom.xpath('//ftp/vfs/data_fs_subdir/text()')[0]
        if len(dom.xpath('//ftp/vfs/add_src/text()')) == 0:
            self.add_src = None
        else:
            self.add_src = dom.xpath('//tftp/add_src/text()')[0].lower()
        self.default_owner = int(dom.xpath('//ftp/vfs/default_owner/text()')[0])
        self.default_group = int(dom.xpath('//ftp/vfs/default_grp/text()')[0])
        self.default_perms = dom.xpath('//ftp/vfs/default_perms/text()')[0]
        # User/Permissions Model related.
        self.user_db = dict()  # user_db[uid] = (user_pass, user_group)
        self.grp_db = dict()   # grp_db[gid] = {group: 'group_name'. users: set(users_uid))
        self._init_user_db()   # Initialize User DB
        self._init_fs()        # Initialize FTP file system.

        # FTP metrics related.
        self.timeout = 30  # set the connection timeout to 300 secs.

    # FIXME: move this method to auth module.
    def _init_user_db(self):
        # TODO: Get users from the template.
        self.user_db[13] = {
            'uname': 'nobody',
            'grp': '45:ftp',
            'password': 'nobody'
        }
        self.user_db[10] = {
            'uname': 'test_user',
            'grp': '45:ftp',
            'password': 'test'
        }
        # Toggle enable/disable anonymous user.
        self.user_db[22] = {
            'uname': 'anonymous',
            'grp': '45:ftp',
            'password': ''
        }
        # FIXME: for testing - remove after use
        self.user_db[3000] = {
            'uname': 'abhinav',
            'grp': '34:abhinav',
            'password': 'abhinav'
        }
        # Let us create groups from the populated users.
        for i in self.user_db.keys():
            grp = self.user_db[i].pop('grp')
            _gid, _gname = grp.split(':')
            _gid = int(_gid)
            if _gid not in self.grp_db.keys():
                # It is a new group. Let us create/register this.
                self.grp_db[_gid] = {'group': _gname, 'users': set()}
            self.grp_db[_gid]['users'].add(i)
        # create a simple set of user and pass combinations for easy auth
        self.user_pass = set(zip([v['uname'] for v in self.user_db.values()],
                                 [v['password'] for v in self.user_db.values()]))

    # FIXME: move this method to auth module.
    def get_uid(self, user_name):
        """Get uid from a username"""
        [_uid] = [k for k, v in self.user_db.items() if user_name in v.values()]
        return _uid

    # FIXME: move this method to auth module.
    def get_gid(self, uid):
        """Get group id of a user from it's uid"""
        [_gid] = [k for k, v in self.grp_db.items() if uid in v['users']]
        return _gid

    def _init_fs(self):
        # Create/register all necessary users and groups in the file system
        _ = {conpot_core.get_vfs().register_user(uid=k, name=v['uname']) for k, v in self.user_db.items()}
        _ = {conpot_core.get_vfs().create_group(gid=k, name=v['group']) for k, v in self.grp_db.items()}
        _ = {conpot_core.get_vfs().add_users_to_group(gid=k, uids=list(v['users'])) for k, v in self.grp_db.items()}
        # Initialize file system
        self.vfs, self.data_fs = conpot_core.add_protocol(protocol_name='ftp',
                                                          data_fs_subdir=self.data_fs_subdir,
                                                          vfs_dst_path=self.root_path,
                                                          src_path=self.add_src,
                                                          owner_uid=self.default_owner,
                                                          group_gid=self.default_group,
                                                          perms=self.default_perms)
        # FIXME: Do chown/chmod here just to be sure.
        self.default_owner = 3000
        self.default_group = 34
        self.vfs.chmod('/', self.default_perms, recursive=True)
        self.vfs.chown('/', uid=self.default_owner, gid=self.default_group, recursive=True)
        if self.add_src:
            logger.info('FTP Serving File System from {} at {} in vfs. FTP data_fs sub directory: {}'.format(
                self.add_src, self.root_path, self.data_fs._sub_dir
            ))
        else:
            logger.info('FTP Serving File System at {} in vfs. FTP data_fs sub directory: {}'.format(
                self.root_path, self.data_fs._sub_dir
            ))
        logger.debug('FTP serving list of files : {}'.format(', '.join(self.vfs.listdir('.'))))
        self.root = '/'  # Setup root dir.
        # check for permissions etc.
        logger.debug("FTP root {} is a directory".format(self.vfs.getcwd() + self.root))
        if self.vfs.access(self.root, 0, R_OK):
            logger.debug("FTP root {} is readable".format(self.vfs.getcwd() + self.root))
        else:
            raise FTPException("FTP root must be readable")
        if self.vfs.access(self.root, 0, W_OK):
            logger.debug("FTP root {} is writable".format(self.vfs.getcwd() + self.root))
        else:
            logger.warning("FTP root {} is not writable".format(self.vfs.getcwd() + self.root))
        # TODO: change permissions for specific files.


@conpot_protocol
class FTPServer(object):
    def __init__(self, template, timeout=5):
        self.timeout = timeout
        self.template = template
        self.server = None  # Initialize later
        # Initialize vfs here..
        self.handler = FTPCommandChannel
        self.handler.config = FTPConfig(self.template)

    def start(self, host, port):
        self.handler.host, self.handler.port = host, port
        connection = (self.handler.host, self.handler.port)
        self.server = StreamServer(connection, self.handler.stream_server_handle)
        logger.info('FTP server started on: {}'.format(connection))
        self.server.serve_forever()

    def stop(self):
        logger.debug('Stopping FTP server')
        self.server.stop()
        del self.handler


# ---- For debugging ----
if __name__ == '__main__':
    # Set vars for connection information
    TCP_IP = '127.0.0.1'
    TCP_PORT = 10001
    import os
    conpot_core.initialize_vfs()
    test_template = os.getcwd() + '/../../templates/default/ftp/ftp.xml'
    server = FTPServer(test_template)
    try:
        server.start(TCP_IP, TCP_PORT)
    except KeyboardInterrupt:
        server.stop()