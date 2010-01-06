#!/usr/bin/env python

import os
import re
import sys
import time
import socket
import datetime
import optparse
import ConfigParser
import GratiaConnector
import XmlBuilder

# Bootstrap hadoop
if 'JAVA_HOME' not in os.environ:
    os.environ['JAVA_HOME'] = '/usr/java/default'

os.environ['CLASSPATH'] = GratiaConnector.gratia_path+"/../common/jlib/xalan.jar"

def configure():
    usage="usage: %prog [-c|--config=] <probe storage config file location>\nProbe config must contain InfoProviderUrl attribute in the dCache section.\nIt may also contain ReportPoolUsage attribute. If set to false , probe will not report pool statistics"
    parser = optparse.OptionParser(usage=usage)
    parser.add_option("-c", "--config", dest="config", help="Config file to use." )
    options, args = parser.parse_args()

    if ( len(sys.argv) < 2 ):
      parser.print_help()
      sys.exit(0)

    config = options.config
    if not os.path.exists(config):
        raise Exception("Config file %s does not exist." % config)
    try:
        open(config, 'r').read()
    except:
        raise Exception("Config file %s exists, but an error occurred when " \
            "trying to read it." % config)
    cp = ConfigParser.ConfigParser()
    cp.read(config)
    return cp

def _get_se(cp):
    try:
        return cp.get('Gratia', 'SiteName')
    except:
        pass
    try:
        return Gratia.Config.get_SiteName()
    except:
        pass
    try:
        return socket.getfqdn()
    except:
        return 'Unknown'

_my_se = None
def get_se(cp):
    global _my_se
    if _my_se:
        return _my_se
    _my_se = _get_se(cp)
    return _my_se


def main():
    cp = configure()

    gConnector = GratiaConnector.GratiaConnector(cp)

    dCacheUrl = cp.get('dCache', 'InfoProviderUrl')

    poolsUsage = None
    try:
      poolsUsage = cp.get('dCache', 'ReportPoolUsage')
    except:
      pass

    if ( dCacheUrl == None ):
       raise Exception("Config file does not contain dCacheInfoUrl attribute")
  
    ynMap = { 'no' : 1 , 'false' : 1 , 'n':1 , '0' : 1 }
    noPoolsArg = ""
 
    if ( poolsUsage != None and ynMap.has_key(poolsUsage.lower())):
       noPoolsArg = "-PARAM nopools 1"

    import time
    timeNow = int(time.time())

    cmd = "java  org.apache.xalan.xslt.Process %s -PARAM now %d -PARAM SE %s -XSL %s/../dCache-storage/create_se_record.xsl -IN %s " % ( noPoolsArg, timeNow, get_se(cp) ,GratiaConnector.gratia_path, dCacheUrl )

    print cmd

    fd = os.popen(cmd)

    result = XmlBuilder.Xml2ObjectBuilder(fd)
     
    for storageRecord in result.get().get():
       gConnector.send(storageRecord)

if __name__ == '__main__':
    main()
