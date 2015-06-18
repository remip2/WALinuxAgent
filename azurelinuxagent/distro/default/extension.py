# Windows Azure Linux Agent
#
# Copyright 2014 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.4+ and Openssl 1.0+
#
import os
import zipfile
import time
import json
import subprocess
import azurelinuxagent.logger as logger
from azurelinuxagent.utils.osutil import OSUtil
import azurelinuxagent.protocol as prot
from azurelinuxagent.event import AddExtensionEvent, WALAEventOperation
from azurelinuxagent.exception import ExtensionError
import azurelinuxagent.utils.fileutil as fileutil
import azurelinuxagent.utils.restutil as restutil
import azurelinuxagent.utils.shellutil as shellutil

ValidExtensionStatus = ['transitioning', 'error', 'success', 'warning']
ValidAggStatus = ['Installing', 'Ready', 'NotReady', 'Unresponsive']

#TODO when extension is disabled. Ready or NotReady?
HandlerStatusToAggStatus = {
        "uninstalled":"NotReady", 
        "installed":"Installing", 
        "disabled":"Ready",
        "enabled":"Ready"
}

class ExtensionHandler(object):

    def process(self):
        protocol = prot.GetDefaultProtocol()

        extSettings = protocol.getExtensions()
        for setting in extSettings:
            #TODO handle extension in parallel
            self.processExtension(protocol, setting) 
    
    def processExtension(self, protocol, setting):
        ext = LoadExtensionInstance(setting)
        if ext is None:
            ext = ExtensionInstance(setting, setting.getVersion())
        try:
            ext.initLog()
            ext.handle()
            aggStatus = ext.getAggStatus()
        except ExtensionError as e:
            logger.Error("Failed to handle extension: {0}-{1}\n {2}", 
                         setting.getName(),
                         setting.getVersion(),
                         e)
            aggStatus = ext.createAggStatus("NotReady", {
                "status": "error",
                "operation": ext.getCurrOperation(), 
                "code" : -1, 
                "formattedMessage": {
                    "lang":"en-US",
                    "message": str(e)
                }
            });
            AddExtensionEvent(name=setting.getName(), isSuccess=False,
                              op=ext.getCurrOperation(), message = str(e))
        protocol.reportExtensionStatus(setting.getName(), 
                                       setting.getVersion(),
                                       aggStatus)


def ParseExtensionDirName(dirName):
    seprator = dirName.rfind('-')
    if seprator < 0:
        raise ExtensionError("Invalid extenation dir name")
    return dirName[0:seprator], dirName[seprator + 1:]

def LoadExtensionInstance(setting):
    """
    Return the highest version instance with the same name
    """
    targetName = setting.getName()
    installedVersion = None
    ext = None
    libDir = OSUtil.GetLibDir()
    for dirName in os.listdir(libDir):
        path = os.path.join(libDir, dirName)
        if os.path.isdir(path) and dirName.startswith(targetName):
            name, version = ParseExtensionDirName(dirName)
            #Here we need to ensure names are exactly the same.
            if name == targetName:
                if installedVersion is None or installedVersion < version:
                    installedVersion = version
    if installedVersion is not None:
        ext = ExtensionInstance(setting, installedVersion, installed=True)
    return ext

class ExtensionInstance(object):
    def __init__(self, setting, currVersion, installed=False):
        self.setting = setting
        self.currVersion = currVersion
        self.libDir = OSUtil.GetLibDir()
        self.installed = installed
        self.enabled = False
        self.currOperation = None
        prefix = "[{0}]".format(self.getFullName())
        self.logger = logger.Logger(logger.DefaultLogger, prefix)
    
    def initLog(self):
        #Init logger appender for extension
        fileutil.CreateDir(self.getLogDir(), mode=0700)
        self.logger.addLoggerAppender(logger.AppenderConfig({
            'type' : 'FILE',
            'level' : 'INFO',
            'file_path' : os.path.join(self.getLogDir(), 
                                       "CommandExecution.log")
        }))
 
    def handle(self):
        self.logger.info("Process extension settings:")
        self.logger.info("  Name: {0}", self.setting.getName())
        self.logger.info("  Version: {0}", self.setting.getVersion())
        
        if self.installed:
            self.logger.info("Installed version:{0}", self.currVersion)
            handlerStatus = self.getHandlerStatus() 
            self.enabled = handlerStatus == "enabled"
            
        state = self.setting.getState()
        if state == 'enabled':
            self.handleEnable()
        elif state == 'disabled':
            self.handleDisable()
        elif state == 'uninstall':
            self.handleDisable()
            self.handleUninstall()
        else:
            raise ExtensionError("Unknown extension state:{0}".format(state))

    def handleEnable(self):
        targetVersion = self.getTargetVersion()
        if self.installed:
            if targetVersion > self.currVersion:
                self.upgrade(targetVersion)        
            elif targetVersion == self.currVersion:
                self.enable()
            else:
                #TODO downgrade is not allowed?
                raise ExtensionError("A newer version has already been installed")
        else:
            if targetVersion > self.setting.getVersion():
                #This will happen when auto upgrade policy is enabled
                self.logger.info("Auto upgrade to new version:{0}", 
                                 targetVersion)
                self.currVersion = targetVersion
            self.download()
            self.initExtensionDir()
            self.install()
            self.enable()

    def handleDisable(self):
        if not self.installed or not self.enabled:
            return
        self.disable()
  
    def handleUninstall(self):
        if not self.installed:
            return
        self.uninstall()

    def upgrade(self, targetVersion):
        self.logger.info("Upgrade from: {0} to {1}", 
                         self.setting.getVersion(),
                         targetVersion)
        self.currOperation=WALAEventOperation.Upgrade
        old = self
        new = ExtensionInstance(self.setting, targetVersion, self.libDir)
        self.logger.info("Download new extension package")
        new.initLog()
        new.download()
        self.logger.info("Initialize new extension directory")
        new.initExtensionDir()

        old.disable()
        self.logger.info("Update new extension")
        new.update()
        old.uninstall()
        man = new.loadManifest()
        if man.isUpdateWithInstall():
            self.logger.info("Install new extension")
            new.install()
        self.logger.info("Enable new extension")
        new.enable()
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=WALAEventOperation.Upgrade, message="")

    def download(self):
        self.logger.info("Download extension package")
        self.currOperation=WALAEventOperation.Download
        uris = self.getPackageUris()
        package = None
        for uri in uris:
            try:
                resp = restutil.HttpGet(uri, chkProxy=True)
                package = resp.read()
                break
            except restutil.HttpError as e:
                self.logger.warn("Failed download extension from: {0}", uri)

        if package is None:
            raise ExtensionError("Download extension failed")
        
        self.logger.info("Unpack extension package")
        packageFile = os.path.join(self.libDir,
                                   os.path.basename(uri) + ".zip")
        fileutil.SetFileContents(packageFile, bytearray(package))
        zipfile.ZipFile(packageFile).extractall(self.getBaseDir())
        chmod = "find {0} -type f | xargs chmod u+x".format(self.getBaseDir())
        shellutil.Run(chmod)
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")
    
    def initExtensionDir(self):
        self.logger.info("Initialize extension directory")
        #Save HandlerManifest.json
        manFile = fileutil.SearchForFile(self.getBaseDir(), 
                                         'HandlerManifest.json')
        man = fileutil.GetFileContents(manFile, removeBom=True)
        fileutil.SetFileContents(self.getManifestFile(), man)    

        #Create status and config dir
        statusDir = self.getStatusDir() 
        fileutil.CreateDir(statusDir, mode=0700)
        configDir = self.getConfigDir()
        fileutil.CreateDir(configDir, mode=0700)
        
        #Init handler state to uninstall
        self.setHandlerStatus("uninstalled")

        #Save HandlerEnvironment.json
        self.createHandlerEnvironment()

    def enable(self):
        self.logger.info("Enable extension.")
        self.currOperation=WALAEventOperation.Enable
        man = self.loadManifest()
        self.launchCommand(man.getEnableCommand())
        self.setHandlerStatus("enabled")
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")

    def disable(self):
        self.logger.info("Disable extension.")
        self.currOperation=WALAEventOperation.Disable
        man = self.loadManifest()
        self.launchCommand(man.getDisableCommand(), timeout=900)
        self.setHandlerStatus("disabled")
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")

    def install(self):
        self.logger.info("Install extension.")
        self.currOperation=WALAEventOperation.Install
        man = self.loadManifest()
        self.launchCommand(man.getInstallCommand(), timeout=900)
        self.setHandlerStatus("installed")
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")

    def uninstall(self):
        self.logger.info("Uninstall extension.")
        self.currOperation=WALAEventOperation.UnInstall
        man = self.loadManifest()
        self.launchCommand(man.getUninstallCommand())
        self.setHandlerStatus("uninstalled")
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")

    def update(self):
        self.logger.info("Update extension.")
        self.currOperation=WALAEventOperation.Update
        man = self.loadManifest()
        self.launchCommand(man.getUpdateCommand(), timeout=900)
        AddExtensionEvent(name=self.setting.getName(), isSuccess=True,
                          op=self.currOperation, message="")
    
    def createAggStatus(self, aggStatus, extStatus, heartbeat=None):
        aggregatedStatus = {
            'handlerVersion' : self.setting.getVersion(),
            'handlerName' : self.setting.getName(),
            'status' : aggStatus,
            'runtimeSettingsStatus' : {
                'settingsStatus' : extStatus,
                'sequenceNumber' : self.setting.getSeqNo()
            }
        }
        if heartbeat is not None:
            aggregatedStatus['code'] = heartbeat['code']
            aggregatedStatus['Message'] = heartbeat['Message']
        return aggregatedStatus

    def getAggStatus(self):
        self.logger.info("Collect extension status")
        extStatus = self.getExtensionStatus()
        self.validateExtensionStatus(extStatus) 

        self.logger.info("Collect handler status")
        handlerStatus = self.getHandlerStatus()
        aggStatus = HandlerStatusToAggStatus[handlerStatus]

        man = self.loadManifest() 
        heartbeat=None
        if man.isReportHeartbeat():
            heartbeat = self.getHeartbeat()
            self.validateHeartbeat(heartbeat)
            aggStatus = heartbeat["status"]

        self.validateAggStatus(aggStatus)
        return self.createAggStatus(aggStatus, extStatus, heartbeat)

    def getExtensionStatus(self):
        extStatusFile = self.getStatusFile()
        try:
            extStatusJson = fileutil.GetFileContents(extStatusFile)
            extStatus = json.loads(extStatusJson)[0]
            return extStatus
        except IOError as e:
            raise ExtensionError("Failed to get status file: {0}".format(e))
        except ValueError as e:
            raise ExtensionError("Malformed status file: {0}".format(e))

    def validateExtensionStatus(self, extStatus):
        #Check extension status format
        if 'status' not in extStatus:
            raise ExtensionError("Malformed status file: missing 'status'");
        if 'status' not in extStatus['status']:
            raise ExtensionError("Malformed status file: missing 'status.status'");
        if 'operation' not in extStatus['status']:
            raise ExtensionError("Malformed status file: missing 'status.operation'");
        if 'code' not in extStatus['status']:
            raise ExtensionError("Malformed status file: missing 'status.code'");
        if 'name' not in extStatus['status']:
            raise ExtensionError("Malformed status file: missing 'status.name'");
        if 'formattedMessage' not in extStatus['status']:
            raise ExtensionError("Malformed status file: missing 'status.formattedMessage'");
        if 'lang' not in extStatus['status']['formattedMessage']:
            raise ExtensionError("Malformed status file: missing 'status.formattedMessage.lang'");
        if 'message' not in extStatus['status']['formattedMessage']:
            raise ExtensionError("Malformed status file: missing 'status.formattedMessage.message'");
        if extStatus['status']['status'] not in ValidExtensionStatus:
            raise ExtensionError("Malformed status file: invalid 'status.status'");
        #if type(extStatus['status']['code']) != int:
        #    raise ExtensionError("Malformed status file: 'status.code' must be int");
   
    def getHandlerStatus(self):
        handlerStatus = "NotInstalled"
        handlerStatusFile = self.getHandlerStateFile()
        try:
            handlerStatus = fileutil.GetFileContents(handlerStatusFile)
            return handlerStatus
        except IOError as e:
            raise ExtensionError("Failed to get handler status: {0}".format(e))

    def setHandlerStatus(self, status):
        handlerStatusFile = self.getHandlerStateFile()
        try:
            fileutil.SetFileContents(handlerStatusFile, status)
        except IOError as e:
            raise ExtensionError("Failed to set handler status: {0}".format(e))

    def validateAggStatus(self, aggStatus):
        if aggStatus not in ValidAggStatus:
            raise ExtensionError(("Invalid aggretated status: "
                                  "{0}").format(aggStatus))

    def getHeartbeat(self):
        self.logger.info("Collect heart beat")
        heartbeatFile = os.path.join(OSUtil.GetLibDir(), 
                                     self.getHeartbeatFile())
        if not os.path.isfile(heartbeatFile):
            raise ExtensionError("Failed to get heart beat file")
        if not self.isResponsive(heartbeatFile):
            return {
                    "status": "Unresponsive",
                    "code": -1,
                    "Message": "Extension heartbeat is not responsive"
            }    
        try:
            heartbeatJson = fileutil.GetFileContents(heartbeatFile)
            heartbeat = json.loads(heartbeatJson)[0]['heartbeat']
        except IOError as e:
            raise ExtensionError("Failed to get heartbeat file:{0}".format(e))
        except ValueError as e:
            raise ExtensionError("Malformed heartbeat file: {0}".format(e))
        return heartbeat

    def validateHeartbeat(self, heartbeat):
        if "status" not in heartbeat:
            raise ExtensionError("Malformed heartbeat file: missing 'status'")
        if "code" not in heartbeat:
            raise ExtensionError("Malformed heartbeat file: missing 'code'")
        if "Message" not in heartbeat:
            raise ExtensionError("Malformed heartbeat file: missing 'Message'")
       
    def isResponsive(self, heartbeatFile):
        lastUpdate=int(time.time()-os.stat(heartbeatFile).st_mtime)
        return  lastUpdate > 600    # not updated for more than 10 min

    def launchCommand(self, cmd, timeout=300):
        self.logger.info("Launch command:{0}", cmd)
        baseDir = self.getBaseDir()
        self.updateSetting()
        try:
            devnull = open(os.devnull, 'w')
            child = subprocess.Popen(baseDir + "/" + cmd, shell=True,
                                     cwd=baseDir, stdout=devnull)
        except Exception as e:
            #TODO do not catch all exception
            raise ExtensionError("Failed to launch: {0}, {1}".format(cmd, e))
    
        retry = timeout / 5
        while retry > 0 and child.poll == None:
            time.sleep(5)
            retry -= 1
        if retry == 0:
            os.kill(child.pid, 9)
            raise ExtensionError("Timeout({0}): {1}".format(timeout, cmd))

        ret = child.wait()
        if ret == None or ret != 0:
            raise ExtensionError("Non-zero exit code: {0}, {1}".format(ret, cmd))
    
    def loadManifest(self):
        manFile = self.getManifestFile()
        try:
            data = json.loads(fileutil.GetFileContents(manFile))
        except IOError as e:
            raise ExtensionError('Failed to load manifest file.')
        except ValueError as e:
            raise ExtensionError('Malformed manifest file.')

        return HandlerManifest(data[0])


    def updateSetting(self):
        #TODO clear old .settings file
        fileutil.SetFileContents(self.getSettingsFile(),
                                 json.dumps(self.setting.getSettings()))

    def createHandlerEnvironment(self):
        env = [{
            "name": self.setting.getName(),
            "version" : self.setting.getVersion(),
            "handlerEnvironment" : {
                "logFolder" : self.getLogDir(),
                "configFolder" : self.getConfigDir(),
                "statusFolder" : self.getStatusDir(),
                "heartbeatFile" : self.getHeartbeatFile()
            }
        }]
        fileutil.SetFileContents(self.getEnvironmentFile(),
                                 json.dumps(env))

    def getTargetVersion(self):
        version = self.setting.getVersion()
        updatePolicy = self.setting.getUpgradePolicy()
        if updatePolicy is None or updatePolicy.lower() != 'auto':
            return version
         
        major = version.split('.')[0]
        if major is None:
            raise ExtensionError("Wrong version format: {0}".format(version))

        versionUris = self.setting.getVersionUris()
        versionUris = filter(lambda x : x["version"].startswith(major + "."), 
                             versionUris)
        versionUris = sorted(versionUris, 
                             key=lambda x: x["version"], 
                             reverse=True)
        if len(versionUris) <= 0:
            raise ExtensionError("Can't find version: {0}.*".format(major))

        return versionUris[0]['version']

    def getPackageUris(self):
        version = self.setting.getVersion()
        versionUris = self.setting.getVersionUris()
        if versionUris is None:
            raise ExtensionError("Package uris is None.")
        
        for versionUri in versionUris:
            if versionUri['version']== version:
                return versionUri['uris']

        raise ExtensionError("Can't get package uris for {0}.".format(version))
    
    def getCurrOperation(self):
        return self.currOperation

    def getFullName(self):
        return "{0}-{1}".format(self.setting.getName(), self.currVersion)

    def getBaseDir(self):
        return os.path.join(OSUtil.GetLibDir(), self.getFullName()) 

    def getStatusDir(self):
        return os.path.join(self.getBaseDir(), "status")

    def getStatusFile(self):
        return os.path.join(self.getStatusDir(), 
                            "{0}.status".format(self.setting.getSeqNo()))

    def getConfigDir(self):
        return os.path.join(self.getBaseDir(), 'config')

    def getSettingsFile(self):
        return os.path.join(self.getConfigDir(), 
                            "{0}.settings".format(self.setting.getSeqNo()))

    def getHandlerStateFile(self):
        return os.path.join(self.getConfigDir(), 'HandlerState')

    def getHeartbeatFile(self):
        return os.path.join(self.getBaseDir(), 'heartbeat.log')

    def getManifestFile(self):
        return os.path.join(self.getBaseDir(), 'HandlerManifest.json')

    def getEnvironmentFile(self):
        return os.path.join(self.getBaseDir(), 'HandlerEnvironment.json')

    def getLogDir(self):
        return os.path.join(OSUtil.GetExtLogDir(), 
                            self.setting.getName(), 
                            self.currVersion)

class HandlerEnvironment(object):
    def __init__(self, data):
        self.data = data
   
    def getVersion(self):
        return self.data["version"]

    def getLogDir(self):
        return self.data["handlerEnvironment"]["logFolder"]

    def getConfigDir(self):
        return self.data["handlerEnvironment"]["configFolder"]

    def getStatusDir(self):
        return self.data["handlerEnvironment"]["statusFolder"]

    def getHeartbeatFile(self):
        return self.data["handlerEnvironment"]["heartbeatFile"]

class HandlerManifest(object):
    def __init__(self, data):
        if data is None or data['handlerManifest'] is None:
            raise ExtensionError('Malformed manifest file.')
        self.data = data

    def getName(self):
        return self.data["name"]

    def getVersion(self):
        return self.data["version"]

    def getInstallCommand(self):
        return self.data['handlerManifest']["installCommand"]

    def getUninstallCommand(self):
        return self.data['handlerManifest']["uninstallCommand"]

    def getUpdateCommand(self):
        return self.data['handlerManifest']["updateCommand"]

    def getEnableCommand(self):
        return self.data['handlerManifest']["enableCommand"]

    def getDisableCommand(self):
        return self.data['handlerManifest']["disableCommand"]

    def isRebootAfterInstall(self):
        #TODO handle reboot after install
        if "rebootAfterInstall" not in self.data['handlerManifest']:
            return False
        return self.data['handlerManifest']["rebootAfterInstall"]

    def isReportHeartbeat(self):
        if "reportHeartbeat" not in self.data['handlerManifest']:
            return False
        return self.data['handlerManifest']["reportHeartbeat"]

    def isUpdateWithInstall(self):
        if "updateMode" not in self.data['handlerManifest']:
            return False
        if "updateMode" in self.data:
            return self.data['handlerManifest']["updateMode"].lower() == "updatewithinstall"
        return False