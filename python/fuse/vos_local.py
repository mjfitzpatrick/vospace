"""A set of Python Classes for connecting to and interacting with a VOSpace
   service.

   Connections to VOSpace are made using a SSL X509 certificat which is
   stored in a .pem file. The certificate is supplied by the user or by the
   CADC credential server
"""

import copy
import errno
import fnmatch
import hashlib
from cStringIO import StringIO
import requests

requests.packages.urllib3.disable_warnings()
import html2text
import logging
import mimetypes
import os
import re
import stat
import string
import sys
import time
import urllib
import urlparse
from xml.etree import ElementTree
from copy import deepcopy
from NodeCache import NodeCache
from __version__ import version

try:
    _unicode = unicode
except NameError:
    # If Python is built without Unicode support, the unicode type
    # will not exist. Fake one.
    class _unicode(object):
        pass

logger = logging.getLogger('vos')
logger.setLevel(logging.ERROR)

if sys.version_info[1] > 6:
    logger.addHandler(logging.NullHandler())

BUFSIZE = 8388608  # Size of read/write buffer
MAX_RETRY_DELAY = 128  # maximum delay between retries
DEFAULT_RETRY_DELAY = 30  # start delay between retries when Try_After not sent by server.
MAX_RETRY_TIME = 900  # maximum time for retries before giving up...
CONNECTION_TIMEOUT = 30  # seconds before HTTP connection should drop, should be less than DAEMON timeout in vofs
SERVER = os.getenv('VOSPACE_WEBSERVICE', 'localhost:8080/vospace-2.0/vospace')
#CADC_GMS_PREFIX = "ivo://cadc.nrc.ca/gms#"
#VOSPACE_CERTFILE = os.getenv("VOSPACE_CERTFILE", os.path.join(os.getenv("HOME", "."), '.ssl/cadcproxy.pem'))
CADC_GMS_PREFIX = "ivo://nvo.caltech/gms#"
VOSPACE_CERTFILE = os.getenv("VOSPACE_CERTFILE", os.path.join(os.getenv("HOME","."), '.ssl/vospaceproxy.pem'))
VOSPACE_ARCHIVE = os.getenv("VOSPACE_ARCHIVE", "vospace")

HEADER_DELEG_TOKEN = 'X-CADC-DelegationToken'
CONNECTION_COUNTER = 0


def compute_md5(filename, block_size=BUFSIZE):
    """
    Given a file compute the MD5 of that file.

    :param filename: name of file to open and compute MD5 for.
    :type filename: str
    :param block_size: size of read blocks to stream through MD5 calculator.
    :type block_size: int
    :return: md5 as hex
    :rtype: hex
    """
    md5 = hashlib.md5()
    with open(filename, 'r') as r:
        while True:
            buf = r.read(block_size)
            if len(buf) == 0:
                break
            md5.update(buf)
    return md5.hexdigest()


class URLParser:
    """ Parse out the structure of a URL.

    There is a difference between the 2.5 and 2.7 version of the
    urlparse.urlparse command, so here I roll my own...
    """

    def __init__(self, url):
        self.scheme = None
        self.netloc = None
        self.args = None
        self.path = None
        m = re.match("(^(?P<scheme>[a-zA-Z]*):)?(//(?P<netloc>[^/]*))?"
                     "(?P<path>/?[^?]*)?(?P<args>\?.*)?", url)
        self.scheme = m.group('scheme')
        self.netloc = m.group('netloc')
        self.path = (m.group('path') is not None and m.group('path')) or ''
        self.args = (m.group('args') is not None and m.group('args')) or ''

    def __str__(self):
        return "[scheme: %s, netloc: %s, path: %s]" % (self.scheme,
                                                       self.netloc, self.path)


class Connection:
    """Class to hold and act on the X509 certificate"""

    def __init__(self, vospace_certfile=None, vospace_token=None, http_debug=False):
        """Setup the Certificate for later usage

        cerdServerURL -- the location of the cadc proxy certificate server
        vospace_certfile -- where to store the certificate, if None then
                         ${HOME}/.ssl or a temporary filename
        vospace_token -- token string (alternative to vospace_certfile)
        http_debug -- set True to generate debug statements

        The user must supply a valid certificate or connection will be 'anonymous'.
        """
        self.http_debug = http_debug

        # tokens trump certs. We should only ever have token or certfile
        # set in order to avoid confusion.
        self.vospace_certfile = None
        self.vospace_token = vospace_token
        if self.vospace_token is None:
            # allow anonymous access if no certfile specified
            if vospace_certfile is not None and not os.access(vospace_certfile, os.F_OK):
                logger.warning(
                    "Could not access certificate at {0}.  Reverting to anonymous.".format(vospace_certfile))
                vospace_certfile = None
            self.vospace_certfile = vospace_certfile

        # create a requests session object that all requests will be made via.
        session = requests.Session()
        if self.vospace_certfile is not None:
            session.cert = (self.vospace_certfile, self.vospace_certfile)
        if self.vospace_token is not None:
            session.headers.update(HEADER_DELEG_TOKEN, self.vospace_token)

        user_agent = 'vos ' + version
        if "vofs" in sys.argv[0]:
            user_agent = 'vofs ' + version
        session.headers.update({"User-Agent": user_agent})
        assert isinstance(session, requests.Session)
        self.session = session

    def get_connection(self, url=None):
        """Create an HTTPSConnection object and return.  Uses the client
        certificate if None given.

        uri  -- a VOSpace uri (vos://cadc.nrc.ca~vospace/path)
        """
        if url is not None:
            raise NotImplemented("Connections are no longer set per URL.")
        return self.session


class Node(object):
    """A VOSpace node"""

    IVOAURL = "ivo://ivoa.net/vospace/core"
    SERVURL = "ivo://nvo.caltech/vospace/core"
    ISLOCKED = SERVURL + "#islocked"

    VOSNS = "http://www.ivoa.net/xml/VOSpace/v2.0"
    XSINS = "http://www.w3.org/2001/XMLSchema-instance"
    TYPE = '{%s}type' % XSINS
    NODES = '{%s}nodes' % VOSNS
    NODE = '{%s}node' % VOSNS
    PROTOCOL = '{%s}protocol' % VOSNS
    PROPERTIES = '{%s}properties' % VOSNS
    PROPERTY = '{%s}property' % VOSNS
    ACCEPTS = '{%s}accepts' % VOSNS
    PROVIDES = '{%s}provides' % VOSNS
    ENDPOINT = '{%s}endpoint' % VOSNS
    TARGET = '{%s}target' % VOSNS
    DATA_NODE = "vos:DataNode"
    LINK_NODE = "vos:LinkNode"
    CONTAINER_NODE = "vos:ContainerNode"

    def __init__(self, node, node_type=None, properties=None, subnodes=None):
        """Create a Node object based on the DOM passed to the init method

        if node is a string then create a node named node of nodeType with
        properties
        """
        self.uri = None
        self.name = None
        self.node = node
        self.target = None
        self.groupread = None
        self.groupwrite = None
        self.is_locked = None
        self.is_public = None
        self.type = None
        self.props = {}
        self.attr = {}
        self.xattr = {}
        self._node_list = None

        if not subnodes:
            subnodes = []
        if not properties:
            properties = {}

        if node_type is None:
            node_type = Node.DATA_NODE

        if type(node) == unicode or type(node) == str:
            node = self.create(node, node_type, properties, subnodes=subnodes)

        if node is None:
            raise LookupError("no node found or created?")

        self.node.set('xmlns:vos', self.VOSNS)
        self.update()

    def __eq__(self, node):
        if not isinstance(node, Node):
            return False

        return self.props == node.props

    def update(self):
        """Update the convience links of this node as we update the xml file"""

        self.type = self.node.get(Node.TYPE)
        if self.type is None:
            # logger.debug("Node type unknown, no node created")
            return None
        if self.type == "vos:LinkNode":
            self.target = self.node.findtext(Node.TARGET)

        self.uri = self.node.get('uri')
        self.name = os.path.basename(self.uri)
        for propertiesNode in self.node.findall(Node.PROPERTIES):
            self.set_props(propertiesNode)
        self.is_public = False
        if self.props.get('ispublic', 'false') == 'true':
            self.is_public = True
        self.is_locked = False
        if self.props.get(Node.ISLOCKED, 'false') == 'true':
            self.is_locked = True
        self.groupwrite = self.props.get('groupwrite', '')
        self.groupread = self.props.get('groupread', '')
        self.setattr()
        self.setxattr()

    def set_property(self, key, value):
        """Given a dictionary of props build a properties sub-element"""
        properties = self.node.find(Node.PROPERTIES)
        uri = "%s#%s" % (Node.IVOAURL, key)
        ElementTree.SubElement(properties, Node.PROPERTY,
                               attrib={'uri': uri, 'readOnly': 'false'}).text = value

    def __str__(self):
        """Convert the Node to a string representation of the Node"""

        class Dummy(object):
            pass

        data = []
        file_handle = Dummy()
        file_handle.write = data.append
        ElementTree.ElementTree(self.node).write(file_handle) #, encoding="UTF-8")
        return "".join(data)

    def setattr(self, attr=None):
        """return a dictionary of attributes associated with the file stored
        at node

        These attributes are determind from the node on VOSpace.
        """
        if not attr:
            attr = {}
        # Get the flags for file mode settings.

        self.attr = {}

        # Only one date provided by VOSpace, so use this as all possible
        # dates.
        string_date = self.props.get('date', None)
        access_time = time.time()
        if not string_date:
            modified_time = access_time
        else:
            # mktime is expecting a localtime but we're sending a UT date, so
            # some correction will be needed
            modified_time = time.mktime(time.strptime(string_date[0:19], '%Y-%m-%dT%H:%M:%S')) #MJG -4
            modified_time = modified_time - time.mktime(time.gmtime()) + time.mktime(time.localtime())
        self.attr['st_ctime'] = attr.get('st_ctime', modified_time)
        self.attr['st_mtime'] = attr.get('st_mtime', modified_time)
        self.attr['st_atime'] = access_time

        # set the MODE by or'ing together all flags from stat
        st_mode = 0
        st_nlink = 1
        if self.type == 'vos:ContainerNode':
            st_mode |= stat.S_IFDIR
            st_nlink = max(2, len(self.get_info_list()) + 2)
            # if getInfoList length is < 0 we have a problem elsewhere, so above hack solves that problem.
        elif self.type == 'vos:LinkNode':
            st_mode |= stat.S_IFLNK
        else:
            st_mode |= stat.S_IFREG
        self.attr['st_nlink'] = st_nlink

        # Set the OWNER permissions: all vospace Nodes have read/write/execute by owner
        st_mode |= stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR

        # Set the GROUP permissions
        if self.props.get('groupwrite', "NONE") != "NONE":
            st_mode |= stat.S_IWGRP
        if self.props.get('groupread', "NONE") != "NONE":
            st_mode |= stat.S_IRGRP
            st_mode |= stat.S_IXGRP

        # Set the OTHER permissions
        if self.props.get('ispublic', 'false') == 'true':
            # If you can read the file then you can execute too.
            # Public does NOT mean writeable.  EVER
            st_mode |= stat.S_IROTH | stat.S_IXOTH

        self.attr['st_mode'] = attr.get('st_mode', st_mode)

        # We set the owner and group bits to be those of the currently running process.
        # This is a hack since we don't have an easy way to figure these out.
        # TODO Come up with a better approach to uid setting
        self.attr['st_uid'] = attr.get('st_uid', os.getuid())
        self.attr['st_gid'] = attr.get('st_uid', os.getgid())

        st_size = int(self.props.get('length', 0))
        self.attr['st_size'] = st_size > 0 and st_size or 0

        self.attr['st_blocks'] = self.attr['st_size'] / 512

    def setxattr(self, attrs=None):
        """Initialize the attributes using the properties sent with the node"""
        if attrs is not None:
            raise NotImplemented("Extended attributes are not set.")
        for key in self.props:
            if key in Client.vosProperties:
                continue
            self.xattr[key] = self.props[key]
        return

    def chwgrp(self, group):
        """Set the groupwrite value for this node"""
        if group is not None and group.count(CADC_GMS_PREFIX) > 3:
            raise AttributeError("Exceeded max of 4 write groups: " +
                                 group.replace(CADC_GMS_PREFIX, ""))
        self.groupwrite = group
        return self.change_prop('groupwrite', group)

    def chrgrp(self, group):
        """Set the groupread value for this node"""
        if group is not None and group.count(CADC_GMS_PREFIX) > 3:
            raise AttributeError("Exceeded max of 4 read groups: " +
                                 group.replace(CADC_GMS_PREFIX, ""))
        self.groupread = group
        return self.change_prop('groupread', group)

    def set_public(self, value):
        # logger.debug("Setting value of ispublic to %s" % (str(value)))
        return self.change_prop('ispublic', value)

    @staticmethod
    def fix_prop(prop):
        """Check if prop is a well formed uri and if not then make into one"""
        (url, tag) = urllib.splittag(prop)
        if tag is None and url in ['title',
                                   'creator',
                                   'subject',
                                   'description',
                                   'publisher',
                                   'contributer',
                                   'date',
                                   'type',
                                   'format',
                                   'identifier',
                                   'source',
                                   'language',
                                   'relation',
                                   'coverage',
                                   'rights',
                                   'availableSpace',
                                   'groupread',
                                   'groupwrite',
                                   'publicread',
                                   'quota',
                                   'length',
                                   'MD5',
                                   'mtime',
                                   'ctime',
                                   'ispublic']:
            tag = url
            url = Node.IVOAURL
            prop = url + "#" + tag

        parts = URLParser(url)
        if parts.path is None or tag is None:
            raise ValueError("Invalid VOSpace property uri: {0}".format(prop))

        return prop

    @staticmethod
    def set_prop():
        """Build the XML for a given node"""
        raise NotImplemented()

    def change_prop(self, key, value):
        """Change the node property 'key' to 'value'.

        :param key: The property key to update
        :type key: str
        :param value: The value to give that property.
        :type value: str,None
        :return True/False depending on if the property value was updated.
        """
        # TODO split into 'set' and 'delete'
        uri = self.fix_prop(key)
        changed = False
        found = False
        properties = self.node.findall(Node.PROPERTIES)
        for props in properties:
            for prop in props.findall(Node.PROPERTY):
                if uri != prop.attrib.get('uri', None):
                    continue
                found = True
                if prop.attrib.get('text', None) == value:
                    break
                changed = True
                if value is None:
                    # this is actually a delete property
                    prop.attrib['xsi:nil'] = 'true'
                    prop.attrib["xmlns:xsi"] = Node.XSINS
                    prop.text = ""
                    self.props[self.get_prop_name(uri)] = None
                else:
                    prop.text = value
            if found:
                return changed
        # must not have had this kind of property already, so set value
        property_node = ElementTree.SubElement(properties, Node.PROPERTY)
        property_node.attrib['readOnly'] = "false"
        property_node.attrib['uri'] = uri
        property_node.text = value
        self.props[self.get_prop_name(uri)] = value
        return changed

    def chmod(self, mode):
        """Set the MODE of this Node...

        translates unix style MODE to voSpace and updates the properties...

        This function is quite limited.  We can make a file publicly
        readable and we can set turn off group read/write permissions,
        that's all. """

        changed = 0

        if mode & stat.S_IROTH:
            changed += self.set_public('true')
        else:
            changed += self.set_public('false')

        if mode & stat.S_IRGRP:
            changed += self.chrgrp(self.groupread)
        else:
            changed += self.chrgrp('')

        if mode & stat.S_IWGRP:
            changed += self.chwgrp(self.groupwrite)
        else:
            changed += self.chwgrp('')

        # logger.debug("%d -> %s" % (changed, changed>0))
        return changed > 0

    def create(self, uri, node_type="vos:DataNode", properties=None, subnodes=None):
        """Build the XML needed to represent a VOSpace node returns an ElementTree representation of the XML

        :param uri: The URI for this node.
        :type uri: str
        :param node_type: the type of VOSpace node, likely one of vos:DataNode, vos:ContainerNode, vos:LinkNode
        :type node_type: str
        :param properties:  a dictionary of the node properties, keys should be single words from the IVOA list
        :type properties: dict
        :param subnodes: Any children to attach to this node, only valid for vos:ContainerNode
        :type subnodes: [Node]
        """
        if not subnodes:
            subnodes = []
        elif node_type != 'vos:ContainerNode':
            raise ValueError("Only Container Nodes can have subnodes")

        if not properties:
            properties = {}

        # Build the root node called 'node'
        node = ElementTree.Element("node")
        node.attrib["xmlns"] = Node.VOSNS
        node.attrib["xmlns:vos"] = Node.VOSNS
        node.attrib[Node.TYPE] = node_type
        node.attrib["uri"] = uri

        # create a properties section
        if 'type' not in properties:
            properties['type'] = mimetypes.guess_type(uri)[0]
        properties_node = ElementTree.SubElement(node, Node.PROPERTIES)
        for prop in properties.keys():
            property_node = ElementTree.SubElement(properties_node, Node.PROPERTY)
            property_node.attrib['readOnly'] = "false"
            property_node.attrib["uri"] = self.fix_prop(prop)
            if properties[prop] is None:
                # Setting the property value to None indicates that this is actually a delete
                property_node.attrib['xsi:nil'] = 'true'
                property_node.attrib["xmlns:xsi"] = Node.XSINS
                property_node.text = ""
            elif len(str(properties[prop])) > 0:
                property_node.text = properties[prop]

        # That's it for link nodes...
        if node_type == "vos:LinkNode":
            return node

        # create accepts
        accepts = ElementTree.SubElement(node, Node.ACCEPTS)

        ElementTree.SubElement(accepts, "view").attrib['uri'] = \
            "%s#%s" % (Node.IVOAURL, "defaultview")

        provides = ElementTree.SubElement(node, Node.PROVIDES)
        ElementTree.SubElement(provides, "view").attrib['uri'] = \
            "%s#%s" % (Node.IVOAURL, 'defaultview')
        ElementTree.SubElement(provides, "view").attrib['uri'] = \
            "%s#%s" % (Node.SERVURL, 'rssview')

        # Only DataNode can have a dataview...
        if node_type == "vos:DataNode":
            ElementTree.SubElement(provides, "view").attrib['uri'] = \
                "%s#%s" % (Node.SERVURL, 'dataview')

        # if this is a container node then add directory contents
        if node_type == "vos:ContainerNode":
            node_list = ElementTree.SubElement(node, Node.NODES)
            for sub_node in subnodes:
                node_list.append(sub_node.node)

        return node

    def isdir(self):
        """Check if target is a container Node"""
        # logger.debug(self.type)
        if self.type == "vos:ContainerNode":
            return True
        return False

    def islink(self):
        """Check if target is a link Node"""
        # logger.debug(self.type)
        if self.type == "vos:LinkNode":
            return True
        return False

    def islocked(self):
        """Check if target state is locked for update/delete."""
        return self.props[Node.ISLOCKED] == "true"

    def get_info(self):
        """Organize some information about a node and return as dictionary"""
        date = self.props.get('date', None) # MJG
        if date:
          date = time.mktime(time.strptime(date[0:19], '%Y-%m-%dT%H:%M:%S'))
#        date = time.mktime(time.strptime(self.props['date'][0:-4],
#                                         '%Y-%m-%dT%H:%M:%S'))
        creator = string.lower(re.search('CN=([^,]*)',
                                         self.props.get('creator', 'CN=unknown_000,'))
                               .groups()[0].replace(' ', '_'))
        perm = []
        for i in range(10):
            perm.append('-')
        perm[1] = 'r'
        perm[2] = 'w'
        if self.type == "vos:ContainerNode":
            perm[0] = 'd'
        if self.type == "vos:LinkNode":
            perm[0] = 'l'
        if self.props.get('ispublic', "false") == "true":
            perm[-3] = 'r'
            perm[-2] = '-'
        write_group = self.props.get('groupwrite', 'NONE')
        if write_group != 'NONE':
            perm[5] = 'w'
        read_group = self.props.get('groupread', 'NONE')
        if read_group != 'NONE':
            perm[4] = 'r'
        is_locked = self.props.get(Node.ISLOCKED, "false")
        return {"permissions": string.join(perm, ''),
                "creator": creator,
                "readGroup": read_group,
                "writeGroup": write_group,
                "isLocked": is_locked,
                "size": float(self.props.get('length', 0)),
                "date": date,
                "target": self.target}

    @property
    def node_list(self):
        """Get a list of all the nodes held to by a ContainerNode return a
           list of Node objects"""
        if self._node_list is None:
            self._node_list = []
            for nodesNode in self.node.findall(Node.NODES):
                for nodeNode in nodesNode.findall(Node.NODE):
                    self.add_child(nodeNode)
        return self._node_list

    def add_child(self, child_element_tree):
        """
        Add a child node to a node list.
        :param child_element_tree: a node to add as a child.
        :type child_element_tree: ElementTree
        :return: Node
        """
        child_node = Node(child_element_tree)
        self.node_list.append(child_node)
        return child_node

    def clear_properties(self):
        logger.debug("clearing properties")
        properties_node_list = self.node.findall(Node.PROPERTIES)
        for properties_node in properties_node_list:
            for property_node in properties_node.findall(Node.PROPERTY):
                key = self.get_prop_name(property_node.get('uri'))
                if key in self.props:
                    del self.props[key]
                properties_node.remove(property_node)
        logger.debug("done clearing properties")
        return

    def get_info_list(self):
        """
        :rtype [(Node, dict)]
        :return a list of tuples containing the (NodeName, Info) about the node and its childern
        """
        info = {}
        for node in self.node_list:
            info[node.name] = node.get_info()
        if self.type == "vos:DataNode":
            info[self.name] = self.get_info()
        return info.items()

    def set_props(self, props):
        """Set the properties of node, given the properties element of that node"""
        for property_node in props.findall(Node.PROPERTY):
            self.props[self.get_prop_name(property_node.get('uri'))] = self.get_prop_value(property_node)
        return

    @staticmethod
    def get_prop_name(prop):
        """parse the property uri and get the name of the property"""
        (url, prop_name) = urllib.splittag(prop)
        if url == Node.IVOAURL:
            return prop_name
        return prop

    @staticmethod
    def get_prop_value(prop):
        """Pull out the value part of node"""
        return prop.text


class VOFile(object):
    """
    A class for managing http connections

    Attributes:
    maxRetries - maximum number of retries when transient errors encountered.
                 When set too high (as the default value is) the number of
                 retries are time limited (max 15min)
    maxRetryTime - maximum time to retry for when transient errors are
                   encountered
    """
    errnos = {404: errno.ENOENT,
              401: errno.EACCES,
              409: errno.EEXIST,
              423: errno.EPERM,
              408: errno.EAGAIN}
    # ## if we get one of these codes, retry the command... ;-(
    retryCodes = (503, 408, 504, 412)

    def __init__(self, url_list, connector, method, size=None,
                 follow_redirect=True, byte_range=None, possible_partial_read=False):
        # MJG: Fix URLs for non-GET calls
        if method != 'GET' and '?' in url_list:
            url_list = url_list[: url_list.rindex('?')]
        self.closed = True
        assert isinstance(connector, Connection)
        self.connector = connector
        self.httpCon = None
        self.timeout = -1
        self.size = size
        self.md5sum = None
        self.totalFileSize = None
        self.maxRetries = 10000
        self.maxRetryTime = MAX_RETRY_TIME
        self.url = None
        self.method = None

        # TODO
        # Make all the calls to open send a list of URLs
        # this should be redone during a cleanup. Basically, a GET might
        # result in multiple URLs (list of URLs) but VOFile is also used to
        # retrieve schema files and other info.

        # All the calls should pass a list of URLs. Make sure that we
        # make a deep copy of the input list so that we don't
        # accidentally modify the caller's copy.
        if isinstance(url_list, list):
            self.URLs = deepcopy(url_list)
        else:
            self.URLs = [url_list]
        self.urlIndex = 0
        self.followRedirect = follow_redirect
        self._fpos = 0
        # initial values for retry parameters
        self.currentRetryDelay = DEFAULT_RETRY_DELAY
        self.totalRetryDelay = 0
        self.retries = 0
        self.fileSize = None
        self.request = None
        self.resp = None
        self.trans_encode = None
        # open the connection
        self._fobj = None
        self.open(self.URLs[self.urlIndex], method, byte_range=byte_range, possible_partial_read=possible_partial_read)

    def tell(self):
        return self._fpos

    def seek(self, offset, loc=os.SEEK_SET):
        if loc == os.SEEK_CUR:
            self._fpos += offset
        elif loc == os.SEEK_SET:
            self._fpos = offset
        elif loc == os.SEEK_END:
            self._fpos = int(self.size) - offset
        return

    @staticmethod
    def flush():
        """
        Flush is a NO OP in VOFile: only really flush on close.
        @return:
        """
        return

    def close(self):
        """close the connection."""

        if not self.closed:
            try:
                if self.trans_encode is not None:
                    self.httpCon.send('0\r\n\r\n')
                    logger.debug("End of document sent.")
                logger.debug("getting response.")
                self.resp = self.connector.session.send(self.request)
                logger.debug("checking response status.")
                self.checkstatus()
            finally:
                self.closed = True
        return self.closed

    def checkstatus(self, codes=(200, 201, 202, 206, 302, 303, 503, 416,
                                 416, 402, 408, 412, 504, 204)):
        """check the response status"""
        msgs = {404: "Node Not Found",
                401: "Not Authorized",
                409: "Conflict",
                423: "Locked",
                408: "Connection Timeout"}
        logger.debug("status %d for URL %s" % (self.resp.status_code, self.url))
        if self.resp.status_code not in codes:
            logger.debug("Got status code: %s for %s" %
                         (self.resp.status_code, self.url))
            msg = self.resp.content
            if msg is not None:
                msg = html2text.html2text(msg, self.url).strip().replace('\n', ' ')
            logger.debug("Error message: {0}".format(msg))

            if self.resp.status_code in VOFile.errnos.keys() or (msg is not None and "Node is busy" in msg):
                if msg is None or len(msg) == 0 and self.resp.status_code in msgs:
                    msg = msgs[self.resp.status_code]
                if (self.resp.status_code == 401 and
                        self.connector.vospace_certfile is None and
                        self.connector.vospace_token is None):
                    msg += " using anonymous access "
            exception = IOError(VOFile.errnos.get(self.resp.status_code, self.resp.status_code), msg)
            if self.resp.status_code == 500 and "read-only" in msg:
                exception = IOError(errno.EPERM, "VOSpace in read-only mode.")
            raise exception

        # Get the file size. We use this 'X-CADC-Content-Length' as a
        # fallback to work around a server-side Java bug that limits
        # 'Content-Length' to a signed 32-bit integer (~2 gig files)
        try:
            self.size = int(self.resp.headers.get("Content-Length", self.resp.headers.get("X-CADC-Content-Length", 0)))
        except:
            self.size = 0

        if self.resp.status_code == 200:
            self.md5sum = self.resp.headers.get("Content-MD5", None)
            self.totalFileSize = self.size

        return True

    def open(self, url, method="GET", byte_range=None, possible_partial_read=False):
        """Open a connection to the given URL
        :param url: The URL to be openned
        :type url: str
        :param method: HTTP Method to use on open (PUT/GET/POST)
        :type method: str
        :param byte_range: The range of byte_range to read, This is in open so we can set the header parameter.
        :type byte_range: str
        :param possible_partial_read:  Sometimes we kill during read, this tells the server that isn't an error.
        :type possible_partial_read: bool
        """
        logger.debug("Opening %s (%s)" % (url, method))
        self.url = url
        self.method = method

        request = requests.Request(self.method, url)

        self.trans_encode = None

        # Try to send a content length hint if this is a PUT.
        # otherwise send as a chunked PUT
        if method in ["PUT"]:
            try:
                self.size = int(self.size)
                request.headers.update({"Content-Length": self.size,
                                        "X-CADC-Content-Length": self.size})
            except TypeError:
                self.size = None
                self.trans_encode = "chunked"
        elif method in ["POST", "DELETE"]:
            self.size = None
            self.trans_encode = "chunked"

        if method in ["PUT", "POST", "DELETE"]:
            content_type = "text/xml"
            # Workaround for UWS library issues MJG 
            if 'sync' in URL or 'transfer' in URL:
                content_type = 'application/x-www-form-urlencoded'
            if method == "PUT":
                ext = os.path.splitext(urllib.splitquery(url)[0])[1]
                if ext in ['.fz', '.fits', 'fit']:
                    content_type = 'application/fits'
                else:
                    content_type = mimetypes.guess_type(url)[0]
                    if content_type is None: content_type = "text/xml" # MJG
            if content_type is not None:
                request.headers.update({"Content-Type": content_type})
        if byte_range is not None and method == "GET":
            request.headers.update({"Range": byte_range})
        request.headers.update({"Accept": "*/*",
                                "Expect": "100-continue"})

        # set header if a partial read is possible
        if possible_partial_read and method == "GET":
            request.headers.update({"X-CADC-Partial-Read": "true"})
        try:
            self.request = self.connector.session.prepare_request(request)
        except Exception as ex:
            logger.error(str(ex))

    def get_file_info(self):
        """Return information harvested from the HTTP header"""
        return self.totalFileSize, self.md5sum

    def read(self, size=None):
        """return size bytes from the connection response"""

        if self.resp is None:
            try:
                logger.debug("Intializing read by sending request: {0}".format(self.request))
                self.resp = self.connector.session.send(self.request, stream=True)
                self.checkstatus()
            except Exception as ex:
                logger.debug("Error on read: {}".format(ex))
                raise ex

        if self.resp is None:
            raise IOError(errno.EFAULT, "No response from VOServer")

        read_error = None
        if self.resp.status_code == 416:
            return ""
        # check the most likely response first
        if self.resp.status_code == 200:
            buff = self.resp.raw.read(size)
            size = size is not None and size < len(buff) and size or len(buff)
            logger.debug("Sending back {0} bytes".format(size))
            return buff[:size]
        if self.resp.status_code == 206:
            buff = self.resp.raw.read(size)
            size = size is not None and size < len(buff) and size or len(buff)
            self._fpos += size
            return buff[:size]
        elif self.resp.status_code == 303 or self.resp.status_code == 302:
            url = self.resp.getheader('Location', None)
            logger.debug("Got redirect URL: {0}".format(url))
            self.url = url
            if not url:
                raise IOError(errno.ENOENT,
                              "Got 303 on {0} but no Location value in header? [{1}]".format(self.url,
                                                                                             self.resp.content),
                              self.url)
            if self.followRedirect:
                self.open(url, "GET")
                # logger.debug("Following redirected URL:  %s" % (URL))
                return self.read(size)
            else:
                # logger.debug("Got url:%s from redirect but not following" %
                # (self.url))
                return self.url
        elif self.resp.status_code in VOFile.retryCodes:
            # Note: 404 (File Not Found) might be returned when:
            # 1. file deleted or replaced
            # 2. file migrated from cache
            # 3. hardware failure on storage node
            # For 3. it is necessary to try the other URLs in the list
            #   otherwise this the failed URL might show up even after the
            #   caller tries to re-negotiate the transfer.
            # For 1. and 2., calls to the other URLs in the list might or
            #   might not succeed.
            if self.urlIndex < len(self.URLs) - 1:
                # go to the next URL
                self.urlIndex += 1
                self.open(self.URLs[self.urlIndex], "GET")
                return self.read(size)
        else:
            self.URLs.pop(self.urlIndex)  # remove url from list
            if len(self.URLs) == 0:
                # no more URLs to try...
                if read_error is not None:
                    raise read_error
                if self.resp.status_code == 404:
                    raise IOError(errno.ENOENT, self.url)
                else:
                    raise IOError(errno.EIO,
                                  "unexpected server response %s (%d)" %
                                  (self.resp.reason, self.resp.status_code), self.url)
            if self.urlIndex < len(self.URLs):
                self.open(self.URLs[self.urlIndex], "GET")
                return self.read(size)

        # start from top of URLs with a delay
        self.urlIndex = 0
        logger.error("Servers busy {0} for {1}".format(self.resp.status_code, self.URLs))
        msg = self.resp.content
        if msg is not None:
            msg = html2text.html2text(msg, self.url).strip()
        else:
            msg = "No Message Sent"
        logger.error("Message from VOSpace {0}: {1}".format(self.url, msg))
        try:
            # see if there is a Retry-After in the head...
            ras = int(self.resp.getheader("Retry-After", 5))
        except:
            ras = self.currentRetryDelay
            if (self.currentRetryDelay * 2) < MAX_RETRY_DELAY:
                self.currentRetryDelay *= 2
            else:
                self.currentRetryDelay = MAX_RETRY_DELAY

        if ((self.retries < self.maxRetries) and
                (self.totalRetryDelay < self.maxRetryTime)):
            logger.error("Retrying in {0} seconds".format(ras))
            self.totalRetryDelay += ras
            self.retries += 1
            time.sleep(int(ras))
            self.open(self.URLs[self.urlIndex], "GET")
            return self.read(size)
        else:
            raise IOError(self.resp.status_code,
                          "failed to connect to server after multiple attempts {0} {1}".format(self.resp.reason,
                                                                                               self.resp.status_code),
                          self.url)

    @staticmethod
    def write(buf):
        """write buffer to the connection"""
        raise NotImplemented("Write of buffer to VOFile not implemented")


class Client:
    """The Client object does the work"""

    VOServers = {'nvo.caltech!vospace': SERVER,
                 'nvo.caltech~vospace': SERVER}

    VOTransferEndPoint = '/sync'
    VOPropertiesEndPoint = '/vospace'
    VO_HTTPGET_PROTOCOL = 'ivo://ivoa.net/vospace/core#httpget'
    VO_HTTPPUT_PROTOCOL = 'ivo://ivoa.net/vospace/core#httpput'
    VO_HTTPSGET_PROTOCOL = 'ivo://ivoa.net/vospace/core#httpsget'
    VO_HTTPSPUT_PROTOCOL = 'ivo://ivoa.net/vospace/core#httpsput'
    DWS = '/data/pub/'

    # reserved vospace properties, do not use these for extended property names
    vosProperties = ["description", "type", "encoding", "MD5", "length",
                     "creator", "date", "groupread", "groupwrite", "ispublic"]

    def __init__(self, vospace_certfile=None, root_node=None, conn=None,
                 cadc_short_cut=False, http_debug=False,
                 secure_get=False, vospace_token=None):
        """This could/should be expanded to set various defaults

        :param vospace_certfile: CADC x509 proxy certificate file location. Overrides certfile in conn.
        :type vospace_certfile: str
        :param vospace_token: token string (alternative to vospace_certfile)
        :type vospace_token: str
        :param root_node: the base of the VOSpace for uri references.
        :type root_node: str
        :param conn: a connection pool object for this Client
        :type conn: Session
        :param cadc_short_cut: if True then just assumed data web service urls
        :type cadc_short_cut: bool
        :param http_debug: turn on http debugging.
        :type http_debug: bool
        :param secure_get: Use HTTPS: ie. transfer contents of files using SSL encryption.
        :type secure_get: bool
        """

        if not isinstance(conn, Connection):
            vospace_certfile = vospace_certfile is None and VOSPACE_CERTFILE or vospace_certfile
            conn = Connection(vospace_certfile=vospace_certfile,
                              vospace_token=vospace_token,
                              http_debug=http_debug)

        if conn.vospace_certfile:
            logger.debug("Using certificate file: {0}".format(vospace_certfile))
        if conn.vospace_token:
            logger.debug("Using vospace token: " + conn.vospace_token)

        vospace_certfile = conn.vospace_certfile
        # Set the protocol
        if vospace_certfile is None:
            self.protocol = "http"
        else:
            self.protocol = "https"

        self.conn = conn
        self.VOSpaceServer = "nvo.caltech!vospace"
        self.rootNode = root_node
        self.nodeCache = NodeCache()
        self.cadc_short_cut = cadc_short_cut
        self.secure_get = secure_get
        return

    def glob(self, pathname):
        """Return a list of paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. However, unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns.

        """
        return list(self.iglob(pathname))

    def iglob(self, pathname):
        """Return an iterator which yields the paths matching a pathname pattern.

        The pattern may contain simple shell-style wildcards a la fnmatch. However, unlike fnmatch, filenames
        starting with a dot are special cases that are not matched by '*' and '?' patterns.

        :param pathname: path to run glob against.
        :type pathname: str
        """
        dirname, basename = os.path.split(pathname)
        if not self.has_magic(pathname):
            if basename:
                self.get_node(pathname)
                yield pathname
            else:
                # Patterns ending with a slash should match only directories
                if self.iglob(dirname):
                    yield pathname
            return
        if not dirname:
            for name in self.glob1(self.rootNode, basename):
                yield name
            return
        # `os.path.split()` returns the argument itself as a dirname if it is a
        # drive or UNC path.  Prevent an infinite recursion if a drive or UNC path
        # contains magic characters (i.e. r'\\?\C:').
        if dirname != pathname and self.has_magic(dirname):
            dirs = self.iglob(dirname)
        else:
            dirs = [dirname]
        if self.has_magic(basename):
            glob_in_dir = self.glob1
        else:
            glob_in_dir = self.glob0
        for dirname in dirs:
            for name in glob_in_dir(dirname, basename):
                yield os.path.join(dirname, name)

    # These 2 helper functions non-recursively glob inside a literal directory.
    # They return a list of basenames. `glob1` accepts a pattern while `glob0`
    # takes a literal basename (so it only has to check for its existence).

    def glob1(self, dirname, pattern):
        """

        :param dirname: name of the directory to look for matches in.
        :type dirname: str
        :param pattern: pattern to match directory contents names against
        :type pattern: str
        :return:
        """
        if not dirname:
            dirname = self.rootNode
        if isinstance(pattern, _unicode) and not isinstance(dirname, unicode):
            dirname = unicode(dirname, sys.getfilesystemencoding() or sys.getdefaultencoding())
        try:
            names = self.listdir(dirname, force=True)
        except os.error:
            return []
        if not pattern.startswith('.'):
            names = filter(lambda x: not x.startswith('.'), names)
        return fnmatch.filter(names, pattern)

    def glob0(self, dirname, basename):
        if basename == '':
            # `os.path.split()` returns an empty basename for paths ending with a
            # directory separator.  'q*x/' should match only directories.
            if self.isdir(dirname):
                return [basename]
        else:
            if self.access(os.path.join(dirname, basename)):
                return [basename]
            else:
                raise IOError(errno.EACCES, "Permission denied: {0}".format(os.path.join(dirname, basename)))
        return []

    magic_check = re.compile('[*?[]')

    @classmethod
    def has_magic(cls, s):
        return cls.magic_check.search(s) is not None

    # @logExceptions()
    def copy(self, source, destination, send_md5=False):
        """copy from source to destination.

        One of source or destination must be a vospace location and the other must be a local location.

        :param source: The source file to send to VOSpace or the VOSpace node to retrieve
        :type source: str
        :param destination: The VOSpace location to put the file to or the local destination.
        :type destination: str
        :param send_md5: Should copy send back the md5 of the destination file or just the size?
        :type send_md5: bool

        """
        # TODO: handle vospace to vospace copies.

        success = False
        destination_size = None
        destination_md5 = None
        source_md5 = None

        if source[0:4] == "vos:":
            check_md5 = False
            match = re.search("([^\[\]]*)(\[.*\])$", source)
            if match is not None:
                view = 'cutout'
                source = match.group(1)
                cutout = match.group(2)
            else:
                view = 'data'
                cutout = None
                check_md5 = True
                source_md5 = self.get_node(source).props.get('MD5', 'd41d8cd98f00b204e9800998ecf8427e')

            get_urls = self.get_node_url(source, method='GET', cutout=cutout, view=view)
            if not isinstance(get_urls, list):
                get_urls = [get_urls]
            for get_url in get_urls:
                try:
                    with open(destination, 'w') as fout:
                        fout.write(self.conn.session.get(get_url).content)
                    destination_size = os.stat(destination).st_size
                    if check_md5:
                        destination_md5 = compute_md5(destination)
                        assert destination_md5 == source_md5
                except Exception as ex:
                    logging.debug("Failed to GET {0}".format(get_url))
                    logging.debug("Got error {0}".format(ex))
                    continue
                success = True
                break
        else:
            source_md5 = compute_md5(source)
            put_urls = self.get_node_url(destination, 'PUT')
            if not isinstance(put_urls, list):
                put_urls = [put_urls]
            for put_url in put_urls:
                try:
                    with open(source, 'r') as fin:
                        self.conn.session.put(put_url, data=fin)
                    node = self.get_node(destination, limit=0, force=True)
                    destination_md5 = node.props.get('MD5', 'd41d8cd98f00b204e9800998ecf8427e')
                    assert destination_md5 == source_md5
                except Exception as ex:
                    logging.debug("FAILED to PUT to {0}".format(put_url))
                    logging.debug("Got error: {0}".format(ex))
                    continue
                success = True
                break

        if not success:
            raise IOError(errno.EFAULT, "Failed copying {0} -> {1}".format(source, destination))

        return send_md5 and destination_md5 or destination_size

    def fix_uri(self, uri):
        """given a uri check if the authority part is there and if it isn't
        then add the CADC vospace authority

        """
        parts = URLParser(uri)
        # TODO
        # implement support for local files (parts.scheme=None
        # and self.rootNode=None

        if parts.scheme is None:
            if self.rootNode is not None:
                uri = self.rootNode + uri
            else:
                return uri
        parts = URLParser(uri)
        if parts.scheme != "vos":
            # Just past this back, I don't know how to fix...
            return uri
        # Check that path name compiles with the standard
        logger.debug("Got value of args: {0}".format(parts.args))
        if parts.args is not None and parts.args != "":
            uri = urlparse.parse_qs(urlparse.urlparse(parts.args).query).get('link', None)[0]
            logger.debug("Got uri: {0}".format(uri))
            if uri is not None:
                return self.fix_uri(uri)
        # Check for 'cutout' syntax values.
        path = re.match("(?P<filename>[^\[]*)(?P<ext>(\[\d*:?\d*\])?"
                        "(\[\d*:?\d*,?\d*:?\d*\])?)", parts.path)
        filename = os.path.basename(path.group('filename'))
        if not re.match("^[_\-\(\)=\+!,;:@&\*\$\.\w~]*$", filename):
            raise IOError(errno.EINVAL, "Illegal vospace container name",
                          filename)
        path = path.group('filename')
        # insert the default VOSpace server if none given
        host = parts.netloc
        if not host or host == '':
            host = self.VOSpaceServer
        path = os.path.normpath(path).strip('/')
        return "{0}://{1}/{2}{3}".format(parts.scheme, host, path, parts.args)

    def get_node(self, uri, limit=0, force=False):
        """connect to VOSpace and download the definition of vospace node

        :param uri:   -- a voSpace node in the format vos:/vospaceName/nodeName
        :type uri: str
        :param limit: -- load children nodes in batches of limit
        :type limit: int, None
        :return: The VOSpace Node
        :rtype: Node

        """
        logger.debug("Getting node {0}".format(uri))
        uri = self.fix_uri(uri)
        node = None
        if not force and uri in self.nodeCache:
            node = self.nodeCache[uri]
        if node is None:
            logger.debug("Getting node {0} from ws".format(uri))
            with self.nodeCache.watch(uri) as watch:
                # If this is vospace URI then we can request the node info
                # using the uri directly, but if this a URL then the metadata
                # comes from the HTTP header.
                if uri[0:4] == 'vos:':
                    vo_fobj = self.open(uri, os.O_RDONLY, limit=limit)
                    vo_xml_string = vo_fobj.read()
                    xml_file = StringIO(vo_xml_string)
                    xml_file.seek(0)
                    dom = ElementTree.parse(xml_file)
                    node = Node(dom.getroot())
                elif uri.startswith('http'):
                    header = self.open(None, url=uri, mode=os.O_RDONLY, head=True)
                    header.read()
                    logger.debug("Got http headers: {0}".format(header.resp.headers))
                    properties = {'type': header.resp.getheader('Content-type', 'txt'),
                                  'date': time.strftime(
                                      '%Y-%m-%dT%H:%M:%S GMT',
                                      time.strptime(header.resp.getheader('date', None),
                                                    '%a, %d %b %Y %H:%M:%S GMT')),
                                  'groupwrite': None,
                                  'groupread': None,
                                  'ispublic': URLParser(uri).scheme == 'https' and 'true' or 'false',
                                  'length': header.resp.getheader('content-length', 0)}
                    node = Node(node=uri, node_type=Node.DATA_NODE, properties=properties)
                    logger.debug(str(node))
                else:
                    raise OSError(2, "Bad URI {0}".format(uri))
                watch.insert(node)
                # IF THE CALLER KNOWS THEY DON'T NEED THE CHILDREN THEY
                # CAN SET LIMIT=0 IN THE CALL Also, if the number of nodes
                # on the firt call was less than 500, we likely got them
                # all during the init
                if limit != 0 and node.isdir() and len(node.node_list) > 500:
                    next_uri = None
                    while next_uri != node.node_list[-1].uri:
                        next_uri = node.node_list[-1].uri
                        xml_file = StringIO(self.open(uri, os.O_RDONLY, next_uri=next_uri, limit=limit).read())
                        xml_file.seek(0)
                        next_page = Node(ElementTree.parse(xml_file).getroot())
                        if len(next_page.node_list) > 0 and next_uri == next_page.node_list[0].uri:
                            next_page.node_list.pop(0)
                        node.node_list.extend(next_page.node_list)
        for childNode in node.node_list:
            with self.nodeCache.watch(childNode.uri) as childWatch:
                childWatch.insert(childNode)
        return node

    def get_node_url(self, uri, method='GET', view=None, limit=0, next_uri=None, cutout=None, full_negotiation=None):
        """Split apart the node string into parts and return the correct URL for this node.

        :param uri: The VOSpace uri to get an associated url for.
        :type uri: str
        :param method: What will this URL be used to do: 'GET' the node, 'PUT' or 'POST' to the node or 'DELETE' it
        :type method: str
        :param view: If this is a 'GET' which view of the node should the URL provide.
        :type view: str
        :param limit: If this is a container how many of the children should be returned? (None - Unlimited)
        :type limit: int, None
        :param next_uri: When getting a container we make repeated calls until all 'limit' children returned. next_uri
        tells the service what was the last child uri retrieved in the previous call.
        :type next_uri: str
        :param cutout: The cutout pattern to apply to the file at the service end: applies to view='cutout' only.
        :type cutout: str
        :param full_negotiation: Should we use the transfer UWS or do a GET and follow the redirect.
        :type full_negotiation: bool
        """
        uri = self.fix_uri(uri)
        logger.debug("Getting URL for: " + str(uri))

        parts = URLParser(uri)

        # see if we have a VOSpace server that goes with this URI in our look up list
        server = Client.VOServers.get(parts.netloc, None)
        if server is None:
            # Since we don't know how to get URLs for this server we should just return the uri.
            return uri

        # full_negotiation is an override, so it can be used to force either shortcut (false) or full negotiation (true)
        if full_negotiation is not None:
            do_shortcut = not full_negotiation
        else:
            do_shortcut = self.cadc_short_cut
	do_shortcut = False # MJG

        if not do_shortcut and method == 'GET' and view in ['data', 'cutout']:
            return self._get(uri, view=view, cutout=cutout)

        if not do_shortcut and method == 'PUT':
            return self._put(uri)

        if (view == "cutout" and cutout is None) or (cutout is not None and view != "cutout"):
            raise ValueError("For cutout, must specify a view=cutout and for view=cutout must specify cutout")

        if method == 'GET' and view not in ['data', 'cutout']:
            # This is a request for the URL of the Node, which returns an XML document that describes the node.
            fields = {}
            if limit is not None:
                fields['limit'] = limit
            if view is not None:
                fields['view'] = view
            if next_uri is not None:
                fields['uri'] = next_uri
            data = ""
            if len(fields) > 0:
                data = "?" + urllib.urlencode(fields)
            url = "%s://%s/vospace/nodes/%s%s" % (self.protocol, server, parts.path.strip('/'), data)
            logger.debug("URL: %s (%s)" % (url, method))
            return url

        # This is the CADC shortcut. We do a GET request on the service with the parameters sent as arguments.

        direction = {'GET': 'pullFromVoSpace', 'PUT': 'pushToVoSpace'}

        # On GET verride the protocol to be http (faster) unless a secure_get is requested.
        protocol = {
            'GET': {'https': (self.secure_get and Client.VO_HTTPSGET_PROTOCOL) or Client.VO_HTTPGET_PROTOCOL,
                    'http': Client.VO_HTTPGET_PROTOCOL},
            'PUT': {'https': Client.VO_HTTPSPUT_PROTOCOL,
                    'http': Client.VO_HTTPPUT_PROTOCOL}}

        # build the url for that will request the url that provides access to the node.
        url = "%s://%s%s" % (self.protocol, server, Client.VOTransferEndPoint)
        logger.debug("URL: %s" % url)

        args = {
            'TARGET': self.fix_uri(uri),
            'DIRECTION': direction[method],
            'PROTOCOL': protocol[method][self.protocol],
            'view': view}

        if cutout is not None:
            args['cutout'] = cutout
        params = urllib.urlencode(args)
        headers = {"Content-type": "application/x-www-form-urlencoded",
                   "Accept": "text/plain"}

        response = self.conn.session.get(url, params=params, headers=headers, allow_redirects=False)
        assert isinstance(response, requests.Response)
        logging.debug("Transfer Server said: {0}".format(response.content))

        if response.status_code == 303:
            # Normal case is a redirect
            url = response.headers.get('Location', None)
        elif response.status_code == 404:
            # The file doesn't exist
            raise IOError(errno.ENOENT, response.content, url)
        elif response.status_code == 409:
            raise IOError(errno.EREMOTE, response.content, url)
        elif response.status_code == 413:
            raise IOError(errno.E2BIG, response.content, url)
        else:
            logger.debug("Reverting to full negotiation")
            return self.get_node_url(uri,
                                     method=method,
                                     view=view,
                                     full_negotiation=True,
                                     limit=limit,
                                     next_uri=next_uri,
                                     cutout=cutout)

        logger.debug("Sending short cut url: {0}".format(url))
        return url

    def link(self, src_uri, link_uri):
        """Make link_uri point to src_uri.

        :param src_uri: the existing resource, either a vospace uri or a http url
        :type src_uri: str
        :param link_uri: the vospace node to create that will be a link to src_uri
        :type link_uri: str
        """
        if self.isdir(link_uri):
            link_uri = os.path.join(link_uri, os.path.basename(src_uri))
        link_node = Node(self.fix_uri(link_uri), node_type="vos:LinkNode")
        ElementTree.SubElement(link_node.node, "target").text = self.fix_uri(src_uri)
        url = self.get_node_url(link_uri)
        data = str(link_node)
        size = len(data)
        self.conn.session.put(url, data=data, size=size)
        return True

    def move(self, src_uri, destination_uri):
        """Move src_uri to destination_uri.  If destination_uri is a containerNode then move src_uri into destination_uri

        :param src_uri: the VOSpace node to be moved.
        :type src_uri: str
        :param destination_uri: the VOSpace location to move to.
        :type destination_uri: str
        :return did the move succeed?
        :rtype bool
        """
        logger.debug("Moving %s to %s" % (src_uri, destination_uri))
        with self.nodeCache.volatile(self.fix_uri(src_uri)), self.nodeCache.volatile(self.fix_uri(destination_uri)):
            transfer = ElementTree.Element("vos:transfer")
            transfer.attrib['xmlns:vos'] = Node.VOSNS
            ElementTree.SubElement(transfer, "vos:target").text = self.fix_uri(src_uri)
            ElementTree.SubElement(transfer, "vos:direction").text = \
                self.fix_uri(destination_uri)
            ElementTree.SubElement(transfer, "vos:keepBytes").text = "false"

            url = "%s://%s%s" % (self.protocol, SERVER, Client.VOTransferEndPoint)
            xml_string = ElementTree.tostring(transfer)
            logging.debug("Sending transfer document: {0}".format(xml_string))
            resp = self.conn.session.post(url,
                                          allow_redirects=False,
                                          data=xml_string,
                                          headers={'Content-Type': "text/xml"})
            transfer_job_url = resp.headers.get('Location', None)
            if transfer_job_url is None:
                raise OSError(errno.EFAULT, "Failed to get transfer job result. Got: {0}".format(resp.content))
            logging.debug("Move job at: {0}".format(transfer_job_url))
            if transfer_job_url is not None and not self.get_transfer_error(transfer_job_url, src_uri):
                return True
        return False

    def _get(self, uri, view="defaultview", cutout=None):
        if view is None:
            view = "defaultview"
        return self.transfer(uri, "pullFromVoSpace", view, cutout)

    def _put(self, uri):
        return self.transfer(uri, "pushToVoSpace")

    def transfer(self, uri, direction, view="defaultview", cutout=None):
        """Build the transfer XML document"""
        protocol = {"pullFromVoSpace": "{0}get".format(self.protocol),
                    "pushToVoSpace": "{0}put".format(self.protocol)}

        views = {"defaultview": "%s#%s" % (Node.IVOAURL, "defaultview"),
                 "data": "ivo://nvo.caltech/vospace/view#data",
                 "cutout": "ivo://nvo.caltech/vospace/view#cutout"}

        transfer_xml = ElementTree.Element("vos:transfer")
        transfer_xml.attrib['xmlns:vos'] = Node.VOSNS
        ElementTree.SubElement(transfer_xml, "vos:target").text = uri
        ElementTree.SubElement(transfer_xml, "vos:direction").text = direction
        view = views["defaultview"] # MJG
        ElementTree.SubElement(transfer_xml, "vos:view").attrib['uri'] = \
            views.get(view, views["defaultview"])
        if cutout is not None:
            ElementTree.SubElement(transfer_xml, "cutout").attrib['uri'] = \
                cutout
        ElementTree.SubElement(transfer_xml, "vos:protocol").attrib['uri'] = \
            "%s#%s" % (Node.IVOAURL, protocol[direction])
        logger.debug(ElementTree.tostring(transfer_xml))
        url = "%s://%s%s" % (self.protocol, SERVER, Client.VOTransferEndPoint)
        data = ElementTree.tostring(transfer_xml)
        resp = self.conn.session.post(url,
                                      data=data,
                                      allow_redirects=False,
                                      headers={'Content-Type': 'text/xml'})
        logging.debug("{0}".format(resp))
        logging.debug("{0}".format(resp.content))
        if resp.status_code != 303:
            raise OSError(resp.status_code, "Failed to get transfer service response.")
        transfer_url = resp.headers.get('Location', None)
        logger.debug("Got back from transfer URL: %s" % transfer_url)
        xml_string = self.conn.session.get(transfer_url).content
        transfer_document = ElementTree.fromstring(xml_string)
        logger.debug("Transfer Document: %s" % transfer_document)
        all_protocols = transfer_document.findall(Node.PROTOCOL)
        if all_protocols is None or not len(all_protocols) > 0:
            return self.get_transfer_error(transfer_url, uri)

        result = []
        for protocol in all_protocols:
            for node in protocol.findall(Node.ENDPOINT):
                result.append(node.text)
        return result

    def get_transfer_error(self, url, uri):
        """Follow a transfer URL to the Error message"""
        error_codes = {'NodeNotFound': errno.ENOENT,
                       'RequestEntityTooLarge': errno.E2BIG,
                       'PermissionDenied': errno.EACCES,
                       'OperationNotSupported': errno.EOPNOTSUPP,
                       'InternalFault': errno.EFAULT,
                       'ProtocolNotSupported': errno.EPFNOSUPPORT,
                       'ViewNotSupported': errno.ENOSYS,
                       'InvalidArgument': errno.EINVAL,
                       'InvalidURI': errno.EFAULT,
                       'TransferFailed': errno.EIO,
                       'DuplicateNode.': errno.EEXIST,
                       'NodeLocked': errno.EPERM}
        job_url = str.replace(url, "/results/transferDetails", "")

        try:
            phase_url = job_url + "/phase"
            sleep_time = 1
            roller = ('\\', '-', '/', '|', '\\', '-', '/', '|')
            phase = VOFile(phase_url, self.conn, method="GET",
                           follow_redirect=False).read()
            # do not remove the line below. It is used for testing
            logger.debug("Job URL: " + job_url + "/phase")
            while phase in ['PENDING', 'QUEUED', 'EXECUTING', 'UNKNOWN']:
                # poll the job. Sleeping time in between polls is doubling
                # each time until it gets to 32sec
                total_time_slept = 0
                if sleep_time <= 32:
                    sleep_time *= 2
                slept = 0
                if logger.getEffectiveLevel() == logging.INFO:
                    while slept < sleep_time:
                        sys.stdout.write("\r%s %s" % (phase,
                                                      roller[total_time_slept % len(roller)]))
                        sys.stdout.flush()
                        slept += 1
                        total_time_slept += 1
                        time.sleep(1)
                    sys.stdout.write("\r                    \n")
                else:
                    time.sleep(sleep_time)
                phase = self.conn.session.get(phase_url, follow_redirect=False).read()
                logger.debug("Async transfer Phase for url %s: %s " % (url, phase))
        except KeyboardInterrupt:
            # abort the job when receiving a Ctrl-C/Interrupt from the client
            logger.error("Received keyboard interrupt")
            self.conn.session.post(job_url + "/phase",
                                   allow_redirects=False,
                                   data="PHASE=ABORT",
                                   headers={"Content-type": 'text/text'})
            raise KeyboardInterrupt
        status = VOFile(phase_url, self.conn, method="GET",
                        follow_redirect=False).read()

        logger.debug("Phase:  {0}".format(status))
        if status in ['COMPLETED']:
            return False
        if status in ['HELD', 'SUSPENDED', 'ABORTED']:
            # re-queue the job and continue to monitor for completion.
            raise OSError("UWS status: {0}".format(status), errno.EFAULT)
        error_url = job_url + "/error"
        error_message = self.conn.session.get(error_url).content
        logger.debug("Got transfer error {0} on URI {1}".format(error_message, uri))
        # Check if the error was that the link type is unsupported and try and follow that link.
        target = re.search("Unsupported link target:(?P<target> .*)$", error_message)
        if target is not None:
            return target.group('target').strip()
        raise OSError(error_codes.get(error_message, errno.EFAULT),
                      "{0}: {1}".format(uri, error_message))

    def open(self, uri, mode=os.O_RDONLY, view=None, head=False, url=None,
             limit=None, next_uri=None, size=None, cutout=None, byte_range=None,
             full_negotiation=False, possible_partial_read=False):
        """Create a VOFile connection to the specified uri or url.

        :rtype : VOFile
        :param uri: The uri of the VOSpace resource to create a connection to, override by specifying url
        :type uri: str, None
        :param mode: The mode os.O_RDONLY or os.O_WRONLY to open the connection with.
        :type mode: bit
        :param view: The view of the VOSpace resource, one of: default, data, cutout
        :type view: str, None
        :param head: Just return the http header of this request.
        :type head: bool
        :param url: Ignore the uri (ie don't look up the url using get_node_url) and just connect to this url
        :type url: str, None
        :param limit: limit response from vospace to this many child nodes. relevant for containerNode type
        :type limit: int, None
        :param next_uri: The  uri of the last child node returned by a previous request on a containerNode
        :type next_uri: str, None
        :param size: The size of file to expect or be put to VOSpace
        :type size: int, None
        :param cutout: The cutout pattern to use during a get
        :type cutout: str, None
        :param byte_range: The range of bytes to request, rather than getting the entire file.
        :type byte_range: str, None
        :param full_negotiation: force this interaction to use the full UWS interaction to get the url for the resource
        :type full_negotiation: bool
        :param possible_partial_read:
        """

        # sometimes this is called with mode from ['w', 'r']
        # really that's an error, but I thought I'd just accept those are
        # os.O_RDONLY

        if type(mode) == str:
            mode = os.O_RDONLY

        # the url of the connection depends if we are 'getting', 'putting' or
        # 'posting'  data
        method = None
        if mode == os.O_RDONLY:
            method = "GET"
        elif mode & (os.O_WRONLY | os.O_CREAT):
            method = "PUT"
        elif mode & os.O_APPEND:
            method = "POST"
        elif mode & os.O_TRUNC:
            method = "DELETE"
        if head:
            method = "HEAD"
        if not method:
            raise IOError(errno.EOPNOTSUPP, "Invalid access mode", mode)

        if uri is not None and view in ['data', 'cutout']:
            # Check if this is a target node.
            try:
                node = self.get_node(uri)
                if node.type == "vos:LinkNode":
                    target = node.node.findtext(Node.TARGET)
                    logger.debug("%s is a link to %s" % (node.uri, target))
                    if target is None:
                        raise IOError(errno.ENOENT, "No target for link")
                    else:
                        if re.search("^vos://(.+\.)+(.+)+[!~]vospace", target) is not None:
                            # This is a link to another VOSpace node so lets open that instead.
                            return self.open(target, mode, view, head, url, limit,
                                             next_uri, size, cutout, byte_range)
                        else:
                            # A target external to VOSpace, open the target directly
                            # TODO Need a way of passing along authentication.
                            if cutout is not None:
                                target = "{0}?cutout={1}".format(target, cutout)
                            return VOFile([target],
                                          self.conn,
                                          method=method,
                                          size=size,
                                          byte_range=byte_range,
                                          possible_partial_read=possible_partial_read)
            except IOError as e:
                if e.errno in [2, 404]:
                    pass
                else:
                    raise e

        if url is None:
            url = self.get_node_url(uri, method=method, view=view,
                                    limit=limit, next_uri=next_uri, cutout=cutout,
                                    full_negotiation=full_negotiation)
            if url is None:
                raise IOError(errno.EREMOTE)

        return VOFile(url, self.conn, method=method, size=size, byte_range=byte_range,
                      possible_partial_read=possible_partial_read)

    def add_props(self, node):
        """Given a node structure do a POST of the XML to the VOSpace to
           update the node properties"""
        # Make a copy of currnet local state, then get a copy of what's on the server and then update just the changes.
        new_props = copy.deepcopy(node.props)
        old_props = self.get_node(node.uri, force=True).props
        for prop in old_props:
            if prop in new_props and old_props[prop] == new_props[prop] and old_props[prop] is not None:
                del (new_props[prop])
        node.node = node.create(node.uri, node_type=node.type,
                                properties=new_props)
        # Now write these new properties to the node location.
        url = self.get_node_url(node.uri, method='GET')
        data = str(node)
        size = len(data)
        self.conn.session.post(url,
                               headers={'size': size},
                               data=data)

    def create(self, node):
        url = self.get_node_url(node.uri, method='PUT')
        data = str(node)
        size = len(data)
        self.conn.session.put(url, data=data, headers={'size': size})
        return True

    def update(self, node, recursive=False):
        """Updates the node properties on the server. For non-recursive
           updates, node's properties are updated on the server. For
           recursive updates, node should only contain the properties to
           be changed in the node itself as well as all its children. """
        # Let's do this update using the async transfer method
        url = self.get_node_url(node.uri)
        if recursive:
            property_url = "%s://%s%s" % (self.protocol, SERVER,
                                          Client.VOPropertiesEndPoint)
            logger.debug("prop URL: {0}".format(property_url))
            try:
                resp = self.conn.session.post(property_url,
                                              allow_redirects=False,
                                              data=str(node),
                                              headers={'Content-type': 'text/xml'})
            except Exception as ex:
                logger.error(str(ex))
                raise ex
            if resp is None:
                raise OSError(errno.EFAULT, "Failed to connect VOSpace")
            logger.debug("Got prop-update response: {0}".format(resp.content))
            transfer_url = resp.headers.get('Location', None)
            logger.debug("Got job status redirect: {0}".format(transfer_url))
            # logger.debug("Got back %s from $Client.VOPropertiesEndPoint " % (con))
            # Start the job
            self.conn.session.post(transfer_url + "/phase",
                                   allow_redirects=False,
                                   data="PHASE=RUN",
                                   headers={'Content-type': "text/text"})
            self.get_transfer_error(transfer_url, node.uri)
        else:
            resp = self.conn.session.post(url,
                                          data=str(node),
                                          allow_redirects=False)
            logger.debug("update response: {0}".format(resp.content))
        return 0

    def mkdir(self, uri):
        node = Node(self.fix_uri(uri), node_type="vos:ContainerNode")
        url = self.get_node_url(uri)
        response = self.conn.session.put(url, data=str(node))
        return response.status_code == 200

    def delete(self, uri):
        """Delete the node"""
        logger.debug("delete {0}".format(uri))
        with self.nodeCache.volatile(self.fix_uri(uri)):
            url = self.get_node_url(uri, method='GET')
            return self.conn.session.delete(url)

    def get_info_list(self, uri):
        """Retrieve a list of tuples of (NodeName, Info dict)"""
        info_list = {}
        logger.debug(str(uri))
        node = self.get_node(uri, limit=None)
        logger.debug(str(node))
        while node.type == "vos:LinkNode":
            uri = node.target
            try:
                node = self.get_node(uri, limit=None)
            except Exception as e:
                logger.error(str(e))
                break
        for thisNode in node.node_list:
            info_list[thisNode.name] = thisNode.get_info()
        if node.type in ["vos:DataNode", "vos:LinkNode"]:
            info_list[node.name] = node.get_info()
        return info_list.items()

    def listdir(self, uri, force=False):
        """
        Walk through the directory structure a la os.walk.
        Setting force=True will make sure no caching of results are used.
        :rtype [str]
        """
        # logger.debug("getting a listing of %s " % (uri))
        names = []
        logger.debug(str(uri))
        node = self.get_node(uri, limit=None, force=force)
        while node.type == "vos:LinkNode":
            uri = node.target
            # logger.debug(uri)
            node = self.get_node(uri, limit=None, force=force)
        for thisNode in node.node_list:
            names.append(thisNode.name)
        return names

    def isdir(self, uri):
        """Check to see if the given uri points at a containerNode or is
           a link to one.

           uri: a vospace URI
           """
        try:
            node = self.get_node(uri, limit=0)
            while node.type == "vos:LinkNode":
                uri = node.target
                if uri[0:4] == "vos:":
                    node = self.get_node(uri, limit=0)
                else:
                    return False
            if node.type == "vos:ContainerNode":
                return True
        except:
            pass
        return False

    def isfile(self, uri):
        try:
            return self.status(uri)
        except:
            return False

    def access(self, uri, mode=os.O_RDONLY):
        """Test if the give vospace uri can be access if the given mode.

        uri:  a vospace location.
        mode: os.O_RDONLY

        """
        try:
            vofile = self.open(uri, mode=mode)
            return isinstance(vofile, VOFile)
        except Exception as ex:
            logger.debug("Exception on access check.  mode: {0} Exception: {1}".format(mode, ex))
            return False

    def status(self, uri, code=None):
        """Check to see if this given uri points at a containerNode.

        This is done by checking the view=data header and seeing if you
        get an error.
        """
        if not code:
            raise NotImplemented("Use of 'code' values nolonger supported.")
        try:
            node = self.get_node(uri)
            return node.type == 'vos:ContainerNode'
        except Exception as ex:
            logger.debug("Exception on status check: {0}".format(ex))

    def get_job_status(self, url):
        """ Returns the status of a job """
        return VOFile(url, self.conn, method="GET", follow_redirect=False).read()
