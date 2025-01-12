#!/usr/bin/python
#
# condor_meter - Python-based Condor probe for Gratia
#       By Brian Bockelman; Nov 4, 2011
#

from __future__ import print_function

import os
import pwd
import re
import sys
import time
import types
import signal
import random
import os.path
import optparse
import subprocess

from typing import List, Tuple

from gratia.common.Gratia import DebugPrint
from gratia.common.debug import DebugPrintTraceback
from gratia.common import GratiaCore
from gratia.common import GratiaWrapper
from gratia.common import Gratia
from gratia.common import file_utils
from gratia.common import config
from gratia.common import utils

import htcondor
import classad as classadLib

# HACK: allow case-insensitive `attr in ad` checks
# https://jira.opensciencegrid.org/browse/SOFTWARE-3017
# https://github.com/opensciencegrid/gratia-probe/pull/24
if 'name' not in classadLib.ClassAd({"Name": 123}):
    classadLib.ClassAd.__contains__ = lambda ad,attr: ad.get(attr) is not None

g_alternate_records = {}
g_probe_config = None

prog_version = "%%%RPMVERSION%%%"
max_batch_size = 500

min_start_time = time.time() - 120*86400

# The preferred order of JobAd attributes to use for determining the number of
# processors available to the pilot or payload job
PROC_ATTRS = [
    'MachineAttrCpus0',
    'MATCH_EXP_JOB_GLIDEIN_Cpus',
    'CpusProvisioned',
    'RequestCpus'
]

RESOURCE_NAME_ATTRS = [
    'MachineAttrGLIDEIN_ResourceName0',
    'MATCH_EXP_JOBGLIDEIN_ResourceName'
]

# --- classes -------------------------------------------------------------------------

class IgnoreClassadException(Exception):
    pass


# --- functions -----------------------------------------------------------------------


def parse_opts(probe_name):

    probe_config = f"/etc/gratia/{probe_name}/ProbeConfig"

    parser = optparse.OptionParser(usage="""%prog [options] dir1 dir2 

Normal cron usage: $prog --sleep SECONDS

Command line usage: $prog --history
                    $prog --history --start-time=STARTTIME --end-time-ENDTIME""")
    parser.add_option("-f", "--gratia_config", 
        help="Location of the Gratia config; defaults to %s." % probe_config,
        dest="gratia_config",
        default=probe_config)

    parser.add_option("-s", "--sleep", 
        help="""This should be used with normal cron usage. It sets a random 
amount of sleep, up to the specified number of seconds before running.  
This reduces the load on the Gratia collector.""", 
        dest="sleep",
        default=0, type="int")

    parser.add_option("-r", "--history", 
        help="""Process output from condor_history, ignoring the HTCondor 
PER_JOB_HISTORY_DIR directory.  This option should be used with the 
--start-time and --end-time options to reduce the load on the Gratia 
collector.  It will look through all the HTCondor history records and 
attempt to send them to Gratia unless a start and end time are 
specified.""",
        dest="condor_history",
        default=False, action="store_true")

    parser.add_option("--start-time", 
        help="""First time to include when processing records using --history 
option. Time should be formated as YYYY-MM-DD HH:MM:SS where HH:MM:SS 
is assumed to be 00:00:00 if omitted.""", 
        dest="history_start_time",
        default=None)

    parser.add_option("--end-time", 
        help="""Last time to include when processing records using --history 
option. Time should be formated as YYYY-MM-DD HH:MM:SS where HH:MM:SS 
is assumed to be 00:00:00 if omitted""", 
        dest="history_end_time", 
        default=None)    

    parser.add_option("-v", "--verbose", 
        help="Enable verbose logging to stdout.",
        default=False, action="store_true", dest="verbose")

    opts, args = parser.parse_args()

    # Initialize Gratia
    if not opts.gratia_config or not os.path.exists(opts.gratia_config):
        raise Exception("Gratia config, %s, does not exist." % opts.gratia_config)
    GratiaCore.Config = GratiaCore.ProbeConfiguration(opts.gratia_config)

    if opts.verbose:
        GratiaCore.Config.set_DebugLevel(5)

    if not args and not opts.condor_history:
        args = [GratiaCore.Config.getConfigAttribute("DataFolder")]
        DebugPrint(5, "Defaulting processing directory to %s as none are specified on command line" % args[0])

    return opts, args

condor_version_re = re.compile("^\$CondorVersion:\s*(.*)\s*\$\n\$CondorPlatform:\s*(.*)\s*\$")
def getCondorVersion():
    path = GratiaCore.Config.getConfigAttribute("CondorLocation")
    fd = None
    if path:
        cmd = os.path.join(path, "bin", "condor_version")
        if os.path.exists(cmd):
            fd = os.popen(cmd)
        else:
            DebugPrint(0, "Unable to find specified condor_version: %s.  "
                          "Falling back to searching $PATH." % cmd)
    if fd == None:
        fd = os.popen("condor_version")
    version = fd.read()
    if fd.close():
        raise Exception("Unable to invoke condor_version")
    m = condor_version_re.match(version.strip())
    if m:
        return "%s / %s" % m.groups()
    raise Exception("Unable to parse condor_version output: %s" % version)

logfile_re = re.compile("^history\.(?:.*?\#)?\d+\.\d+")
def logfiles_to_process(args):
    for arg in args:
        if os.path.isfile(arg) and os.stat(arg).st_size:
            DebugPrint(5, "Processing logfile %s" % arg)
            yield arg
        elif os.path.isdir(arg):
            DebugPrint(5, "Processing directory %s." % arg)
            for logfile in os.listdir(arg):
                m = logfile_re.match(logfile)
                if m:
                    DebugPrint(5, "Processing logfile %s" % logfile)
                    yield os.path.join(arg, logfile)


def get_num_procs(job_ad):
    """Get the number of processors of a JobAd from one of the following attributes:

    1. MachineAttrCpus0
    2. MATCH_EXP_JOB_GLIDEIN_Cpus
    3. RequestCpus

    If none of the above are set or do not evaluate to an integer, return 0
    """
    procs = 0
    for attr in PROC_ATTRS:
        try:
            procs = int(job_ad.eval(attr))
            break
        except (KeyError, ValueError):
            continue

    return procs

def get_condor_ids(condor_service: str = "") -> Tuple[int, int]:
    """Return the UID/GID of the user running the relevant condor daemons (i.e., IDs specified by CONDOR_IDS or the
    'condor' user).
    condor_service: if 'htcondor-ce' determine the condor user for HTCondor-CE, otherwise use the default condor config
    """
    try:
        if condor_service == 'htcondor-ce':
            os.environ.setdefault("CONDOR_CONFIG", "/etc/condor-ce/condor_config")
            htcondor.reload_config()

        # Admins can specify the UID/GID to run the condor daemons as through the config "CONDOR_IDS = <UID>.<GID>",
        # e.g. CONDOR_IDS = 1.2
        condor_ids = htcondor.param['CONDOR_IDS']
        condor_uid, condor_gid = [int(x) for x in condor_ids.split(".")]
    except KeyError:
        # When custom HTCondor IDs are not specified, they are using the 'condor' user
        try:
            condor_user = pwd.getpwnam('condor')
            condor_gid = condor_user.pw_gid
            condor_uid = condor_user.pw_uid
        except KeyError as exc:
            raise utils.InternalError("ERROR: could not find the 'condor' user") from exc
    except ValueError as exc:
        raise utils.InternalError(f"ERROR: failed to extract UID/GID for the {condor_service} service "
                                  f"from CONDOR_IDS configuration (value: {condor_ids})") from exc
    return condor_uid, condor_gid


def become_condor(condor_service: str = ""):
    """condor_meter is intended to run as a SchedD cron, i.e. as the 'condor' user.
    condor_service: if 'htcondor-ce' determine the condor user for HTCondor-CE, otherwise use the default condor config
    """
    condor_uid, condor_gid = get_condor_ids(condor_service)

    try:
        os.setgid(condor_gid)
        os.setuid(condor_uid)
    except PermissionError as exc:
        raise utils.InternalError("ERROR: failed to drop privileges to the 'condor' user") from exc


condor_history_re = re.compile("^history.(\d+)\.(\d+)")
def main(probe_name):
    if os.getuid() == 0:
        try:
            become_condor(probe_name)  # either 'htcondor-ce' or 'condor-ap'
        except (KeyError, PermissionError) as exc:
            sys.exit(exc)

    try:
        opts, dirs = parse_opts(probe_name)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # Sanity checks for the probe's runtime environment.
    GratiaWrapper.CheckPreconditions()

      
    if opts.sleep:
        rnd = random.randint(1, int(opts.sleep))
        DebugPrint(2, "Sleeping for %d seconds before proceeding." % rnd)
        time.sleep(rnd)

    # Make sure we have an exclusive lock for this probe.
    GratiaWrapper.ExclusiveLock()

    register_gratia()
    GratiaCore.Initialize(opts.gratia_config)
    global g_probe_config
    g_probe_config = opts.gratia_config

    # setup htcondor environment 
    setup_environment()

    # Check to make sure HTCondor config is set correctly
    if not htcondor_configured():
        DebugPrint(-1, "WARNING: HTCondor appears to not be configured correctly. Continuing anyway.")

    if opts.condor_history is True:
        process_using_condor_history(opts.history_start_time, opts.history_end_time)
    else:
        process_history_dirs(dirs)
   
def process_using_condor_history(start_time=None, end_time=None):
    if start_time is not None or end_time is not None:
        # using a start and end date
        DebugPrint(-1, "RUNNING condor_meter MANUALLY using HTCondor history " \
                       "from %s to %s" % (start_time, end_time))
        if start_time is None or end_time is None:
            DebugPrint(-1, "condor_meter --history ERROR: Both --start and " \
                           "--end args are both required")
            sys.exit(1)
        start_time = parse_date(start_time)
        if start_time is None:
            DebugPrint(-1, "condor_meter --history ERROR: Can't parse start time")
            sys.exit(1)
        end_time = parse_date(end_time)
        if end_time is None:
            DebugPrint(-1, "condor_meter --history ERROR: Can't parse end time") 
            sys.exit(1)
        if start_time > end_time:
            DebugPrint(-1, "condor_meter --history ERROR: The end time is after " \
                          "the start time")
            sys.exit(1)
        if start_time > time.time():
            DebugPrint(-1, "condor_meter --history ERROR: The start time is in " \
                           "the future")
            sys.exit(1)
    else:  # using condor history for all dates
        DebugPrint(-1, "RUNNING condor_meter MANUALLY using all HTCondor " \
                       "history")

    process_condor_history(start_time, end_time)
    DebugPrint(-11, "RUNNING condor_meter MANUALLY Finished")

def parse_date(date_string):
    """
    Parse a date/time string in %m-%d-%Y or %m-%d-%Y %H:%M:%S format
    
    Returns None if string can't be parsed, otherwise returns time formatted
    as the number of seconds since the Epoch
    """    
    result = None
    try:
        result = time.strptime(date_string, "%Y-%m-%d %H:%M:%S")        
        return int(round(time.mktime(result)))
    except ValueError:
        pass
    except Exception as e:
        return None

    try:
        result = time.strptime(date_string, "%Y-%m-%d")
        return int(round(time.mktime(result)))
    except ValueError:
        pass
    except Exception as e:
        return None
    
    return result

def register_gratia():
    GratiaCore.RegisterReporter("condor_meter")
    try:
        condor_version = getCondorVersion()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        DebugPrint(0, "Unable to successfully invoke condor_version: %s" %
            str(e))
        sys.exit(1)

    GratiaCore.RegisterService("Condor", condor_version)
    GratiaCore.setProbeBatchManager("condor")

def process_history_dirs(dirs):
    submit_count = 0
    found_count = 0
    alternate_count = 0
    logs_found = 0
    logfile_errors = 0
    # Note we are not ordering logfiles by type, as we don't want to
    # pull them all into memory at once.
    DebugPrint(4, "We will process the following directories: %s." % ", ".join(dirs))
    for log in logfiles_to_process(dirs):
        logs_found += 1
        _, logfile = os.path.split(log)
        # Make sure the filename is in a reasonable format
        m = condor_history_re.match(logfile)
        if m:
            e = None
            try:
                cnt_submit, cnt_found, cnt_alternate = process_history_file(log)
            except ValueError as ex:
                e = ex
                DebugPrint(1, "Failed to parse log file: %s\nError was: %s" % (log, e))
                cnt_submit, cnt_found, cnt_alternate = 0, 0, 0

            if not e and cnt_submit + cnt_alternate == cnt_found and (cnt_submit > 0 or cnt_alternate > 0):
                DebugPrint(5, "Processed %i ClassAds from file %s" % (cnt_submit, log))
            else:
                DebugPrint(2, "Unable to process ClassAd from file (will add to quarantine): %s.  Submit count %d; found count %d" % (log, cnt_submit, cnt_found))
                GratiaCore.QuarantineFile(log, False)
                logfile_errors += 1
            submit_count += cnt_submit
            found_count += cnt_found
            alternate_count += cnt_alternate
        else:
            DebugPrint(2, "Ignoring history file with invalid name: %s" % log)

    DebugPrint(2, "Number of logfiles processed: %d" % logs_found)
    DebugPrint(2, "Number of logfiles with errors: %d" % logfile_errors)
    DebugPrint(2, "Number of usage records submitted: %d" % submit_count)
    DebugPrint(2, "Number of alternate site name usage records: %d" % alternate_count)
    DebugPrint(2, "Number of usage records found: %d" % found_count)
    send_alternate_records(g_alternate_records)

def process_history_file(logfile):

    count_alternate = 0
    count_submit = 0
    count_found = 0
    try:
        fd = open(logfile, 'r')
    except IOError as ie:
        DebugPrint(2, "Cannot process %s: (errno=%d) %s" % (logfile, ie.errno,
            ie.strerror))
        return 0, 0, 0
    added_transient = False

    for classad in fd_to_classad(fd):
        count_found += 1
        if not classad:
            DebugPrint(5, "Ignoring empty classad from file: %s" % logfile)
            continue

        if not added_transient:
            classad['logfile'] = str(logfile)
            added_transient = True
        try:
            r = classadToJUR(classad)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except IgnoreClassadException as e:
            DebugPrint(3, "Ignoring ClassAd: %s" % str(e))
            count_submit += 1
            continue
        except Exception as e:
            DebugPrint(2, "Exception while converting the ClassAd to a JUR: %s" % str(e))
            continue

        enteredStatus = classad.get('EnteredCurrentStatus', 0)
        if classad.get('CompletionDate', 0) == 0:
            classad['CompletionDate'] = enteredStatus

        if classad.get('CompletionDate', min_start_time) < min_start_time:
            DebugPrint(2, "Ignoring too-old job: %s (job age: %s, oldest " \
                "acceptable age: %d)" % (str(classad.get("ClusterId", "Unknown")),
                str(classad.get('CompletionDate', 'MISSING')), min_start_time))
            continue

        if r.GetProbeName() != GratiaCore.Config.get_ProbeName():
            count_alternate += 1
            alt_info = g_alternate_records.setdefault((r.GetProbeName(), r.GetSiteName()), [])
            alt_info.append(r)
            continue

        response = GratiaCore.Send(r)
        if response[:2] == 'OK':
            count_submit += 1

    return count_submit, count_found, count_alternate

def process_condor_history(start_time=None, end_time=None):
    hist_command = None
    if start_time is not None and end_time is not None:
        hist_command = "condor_history -l -constraint " \
                       "'((JobCurrentStartDate > %s) && (JobCurrentStartDate " \
                       "< %s))'" % (start_time, end_time) 
    else:
        hist_command = "condor_history -l"
    DebugPrint(-1, "RUNNING: %s" % hist_command)
    fd = os.popen(hist_command)
    submit_count, found_count, alternate_count = process_history_fd(fd)
    if fd.close():
        DebugPrint(-1, "condor_meter --history ERROR: Call to condor_history " \
                       "failed: %s" % hist_command)

    DebugPrint(-1, "condor_meter --history: Usage records submitted: " \
                   "%d" % submit_count)
    DebugPrint(-1, "condor_meter --history: Usage records found: " \
                   "%d" % found_count)
    DebugPrint(-1, "condor_meter --history: Alternate-site-name usage records" \
                   " found: %d" % alternate_count)
    send_alternate_records(g_alternate_records)

def process_history_fd(fd):
    """
    Process the job history from a file descriptor.  The difference between
    this and process_history_file is that Gratia doesn't have any transient
    files it will attempt to cleanup afterward.
    """
    count_submit = 0
    count_found = 0
    count_alternate = 0
    for classad in fd_to_classad(fd):
        count_found += 1
        if not classad:
            DebugPrint(5, "Ignoring empty classad from file: %s" % fd.name)
            continue

        try:
            r = classadToJUR(classad)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except IgnoreClassadException as e:
            DebugPrint(3, "Ignoring ClassAd: %s" % str(e))
            count_submit += 1
            continue
        except Exception as e:
            DebugPrint(2, "Exception while converting the ClassAd to a JUR: %s" % str(e))
            continue

        enteredStatus = classad.get('EnteredCurrentStatus', 0)
        if classad.get('CompletionDate', 0) == 0:
            classad['CompletionDate'] = enteredStatus

        if classad.get('CompletionDate', min_start_time) < min_start_time:
            DebugPrint(2, "Ignoring too-old job: %s (job age: %s, oldest " \
                "acceptable age: %d)" % (str(classad.get("ClusterId", "Unknown")),
                str(classad.get('CompletionDate', 'MISSING')), min_start_time))
            continue

        if r.GetSiteName() != GratiaCore.Config.get_SiteName():
            count_alternate += 1
            alt_info = g_alternate_records.setdefault((r.GetProbeName(), r.GetSiteName()), [])
            alt_info.append(r)
            continue

        response = GratiaCore.Send(r)
        if response[:2] == 'OK':
            count_submit += 1

    return count_submit, count_found, count_alternate

def setIfExists(func, classad, attr, comment=None, setstr=False):
    if attr in classad:
        val = classad[attr]
        if setstr:
            val = str(val)
        if not comment:
            func(val)
        else:
            func(val, comment)
        return True
    return False

cream_re = re.compile("https://([A-Za-z-.0-9]+):(\d+)/ce-cream/services/CREAM2\s+(\S+)\s+\S+")
def cream_match(match, desired):
    """
    CREAM matches are a bit different.  The desired CE looks like this:
        llrcream.in2p3.fr:8443/cream-pbs
    The match expression looks like this:
        https://llrcream.in2p3.fr:8443/ce-cream/services/CREAM2 pbs cms
    So "normal" matching doesn't work, and we match using this function.
    """
    m = cream_re.match(match)
    if not m:
        return False
    hostname, port, jm = m.groups()
    match2 = "%s:%s/cream-%s" % (hostname, port, jm)
    match2 = match2.strip()
    return match2 == desired


def get_classad_resource_name(classad):
    for attr in RESOURCE_NAME_ATTRS:
        resource_name = classad.get(attr)
        if resource_name:
            return resource_name
    return None


split_re = re.compile(",\s*")
def determine_host_description(classad):
    """
    Determine the value of the host description field.
    This particular field is abused to report glideinWMS-based jobs by
    looking for a particular attribute (MachineAttrGLIDEIN_ResourceName0 or
    MATCH_EXP_JOBGLIDEIN_ResourceName)

    Further, there is logic here from the AAA project to determine if the job
    was an overflow job and adds a "-overflow" suffix.

    If there's no special host description, this returns None.
    """
    resource_name = get_classad_resource_name(classad)
    if resource_name is None:
        return None
    elif resource_name == 'Local Job':
        host_descr = GratiaCore.Config.get_SiteName()
    else:
        host_descr = resource_name

    # Check first for SE-based matching.
    if ('DESIRED_SEs' not in classad) or ('MATCH_GLIDEIN_SEs' not in classad):
        return host_descr
    match_se = classad['MATCH_GLIDEIN_SEs'].strip()
    for desired_se in split_re.split(classad['DESIRED_SEs']):
        desired_se = desired_se.strip()
        if match_se == desired_se:
            return host_descr

    # Then check for CE-based matching.
    if ('DESIRED_Gatekeepers' not in classad) or \
                ('MATCH_GLIDEIN_Gatekeeper' not in classad):
        return host_descr
    match_ce = classad['MATCH_GLIDEIN_Gatekeeper']
    for desired_ce in split_re.split(classad['DESIRED_Gatekeepers']):
        desired_ce = desired_ce.strip()
        if match_ce == desired_ce or cream_match(match_ce, desired_ce):
            return host_descr

    # If neither CE nor SE matched, it must be an overflow job.
    return host_descr + "-overflow"

global_job_id_re = re.compile("(.*)\#\d+\.?\d*\#.*")
campus_factory_usage = re.compile("(.*)\-CF$")
campus_flock_usage = re.compile("(.*)\-Flock$")
def classadToJUR(classad):
    def invalidDuration(classad, field):
        """
        Local function to check duration and make sure it's reasonable.  Values obtained
        from HTCondor team and discussed in SOFTWARE-1132
        """
        if field not in ['RemoteSysCpu', 'RemoteUserCpu']:
            return False      
        if classad[field] < 2000000000:
            return False
        slot_ratio = classad[field] / (float(classad['CumulativeSlotTime']) + 1)
        if slot_ratio > 1000:
            return True
        return False
           

    if 'ClusterId' not in classad:
        DebugPrint(2, "No data passed to classadToJUR: %s" % str(classad))
        raise Exception("No data passed to classadToJUR: %s" % str(classad))
    DebugPrint(5, "Creating JUR for %s" % classad['ClusterId'])

    cmd = os.path.split(classad.get("Cmd", "foo"))[-1]
    if cmd == "condor_dagman":
        if 'logfile' in classad:
            DebugPrint(1, 'Deleting transient condor_dagman input file: '+classad["logfile"])
            file_utils.RemoveFile(classad["logfile"])
        raise IgnoreClassadException("Ignoring classad for condor_dagman monitor.")

    resource_type = "Batch"
    if classad.get("GridMonitorJob", False):
        resource_type = "GridMonitor"
    elif get_classad_resource_name(classad) is not None:
        resource_type = 'BatchPilot'
    r = Gratia.UsageRecord(resource_type)

    r.GlobalJobId(classad.get("UniqGlobalJobId", ""))

    job_id = 'Unknown'
    if "ProcId" in classad and int(classad["ProcId"]) > 0:
        r.LocalJobId("%s.%s" % (classad["ClusterId"], classad["ProcId"]))
        job_id = "%s.%s" % (classad["ClusterId"], classad["ProcId"])
    else:
        r.LocalJobId(str(classad['ClusterId']))
        job_id = classad['ClusterId']

    # I don't think ProcessId was ever correct - used to take the UDP port 
    # from the LastClaimId?

    # a pre-routed job's AuthToken attrs are copied with "orig_" prefix
    # in the routed job (SOFTWARE-5185, HTCONDOR-1071)
    setIfExists(r.LocalUserId, classad, "Owner")
    setIfExists(r.GlobalUsername, classad, "User")
    setIfExists(r.DN, classad, 'x509userproxysubject') or \
    setIfExists(r.DN, classad, 'orig_AuthTokenSubject')
    setIfExists(r.VOName, classad, 'x509UserProxyFirstFQAN') or \
    setIfExists(r.VOName, classad, 'orig_AuthTokenIssuer')  # VO info for SciToken
    setIfExists(r.ReportableVOName, classad, 'x509UserProxyVOName') or \
    setIfExists(r.ReportableVOName, classad, 'orig_AuthTokenIssuer')

    if 'GlobalJobId' in classad:
        r.JobName(classad["GlobalJobId"])
        job_id = classad["GlobalJobId"]
        m = global_job_id_re.match(classad['GlobalJobId'])
        if m:
            submit_host = m.groups()[0]
            r.MachineName(submit_host)
            r.SubmitHost(submit_host)

    setIfExists(r.Status, classad, 'ExitStatus', "Condor Exit Status")

    setIfExists(r.WallDuration, classad, 'RemoteWallClockTime', "Was entered in seconds")

    if 'RemoteUserCpu' in classad:
        if  invalidDuration(classad, 'RemoteUserCpu'):
            DebugPrint(1, 
                       "WARNING: INVALID DATA: Record for %s has invalid " \
                       "RemoteUserCpu time %s, replacing value with " \
                       "0\n" % (job_id, classad['RemoteUserCpu']))
            classad['RemoteUserCpu'] = 0
        r.TimeDuration(classad['RemoteUserCpu'], "RemoteUserCpu")
    else:
        classad['RemoteUserCpu'] = 0

    if 'LocalUserCpu' in classad:
        r.TimeDuration(classad['LocalUserCpu'], 'LocalUserCpu')
    else:
        classad['LocalUserCpu'] = 0

    if 'RemoteSysCpu' in classad:
        if  invalidDuration(classad, 'RemoteSysCpu'):
            DebugPrint(1, 
                       "WARNING: INVALID DATA: Record for %s has invalid " \
                       "RemoteSysCpu time %s, replacing value with " \
                       "0\n" % (job_id, classad['RemoteSysCpu']))                       
            classad['RemoteSysCpu'] = 0
        r.TimeDuration(classad['RemoteSysCpu'], 'RemoteSysCpu')
    else:
        classad['RemoteSysCpu'] = 0

    if 'LocalSysCpu' in classad:
        r.TimeDuration(classad['LocalSysCpu'], 'LocalSysCpu')
    else:
        classad['LocalSysCpu'] = 0

    setIfExists(r.TimeDuration, classad, 'CumulativeSuspensionTime', 'CumulativeSuspensionTime')
    setIfExists(r.TimeDuration, classad, 'CommittedSuspensionTime', 'CommittedSuspensionTime')
    setIfExists(r.TimeDuration, classad, 'CommittedTime', 'CommittedTime')

    classad['SysCpuTotal'] = classad['RemoteSysCpu'] + classad['LocalSysCpu']
    r.CpuDuration(classad['SysCpuTotal'], "system", "Was entered in seconds")

    classad['UserCpuTotal'] = classad['RemoteUserCpu'] + classad['LocalUserCpu']
    r.CpuDuration(classad['UserCpuTotal'], "user", "Was entered in seconds")

    if 'CompletionDate' in classad and classad['CompletionDate'] > 0:
        if hasattr(classad, 'eval'):
            DebugPrint(5, "Current completion time: %s" % classad.eval('CompletionDate'))
            r.EndTime(classad.eval('CompletionDate'), "Was entered in seconds")
        else:
            DebugPrint(5, "Current completion time: %s" % classad['CompletionDate'])
            r.EndTime(classad['CompletionDate'], "Was entered in seconds")

    setIfExists(r.StartTime, classad, 'JobStartDate', "Was entered in seconds")

    setIfExists(r.QueueTime, classad, 'QDate', "Was entered in seconds")

    if 'LastRemoteHost' in classad:
        host = classad['LastRemoteHost'].split("@")[-1]
        host_descr = determine_host_description(classad)
        if host_descr:
            r.Host(host, True, host_descr)
        else:
            r.Host(host, True)

    setIfExists(r.Queue, classad, "JobUniverse", "Condor's JobUniverse field", setstr=True)
    setIfExists(r.NodeCount, classad, 'MaxHosts', "max")

    ########################################################################################
    ########################################################################################
    # Code added to send to arbitrary Ads SOFTWARE-2714
    ArbitraryJobAttrs = str(GratiaCore.Config.getConfigAttribute("ExtraAttributes"))
    DebugPrint(5, "Arbitrary Job Attributes: %s" % ArbitraryJobAttrs)
    ArbitraryAttrslist = re.split(r'[,\s]+', ArbitraryJobAttrs)
    DebugPrint(0, "ArbritaryList: %s" % ArbitraryAttrslist)
    for arbitraryAttr in ArbitraryAttrslist:
        if arbitraryAttr in classad:
            DebugPrint(5, "Arbitrary attribute: %s found with value %s" % (arbitraryAttr, classad.eval(arbitraryAttr)))
            r.AdditionalInfo(arbitraryAttr, classad.eval(arbitraryAttr))
    ########################################################################################
    r.Processors(get_num_procs(classad), metric="max")

    # Set the Gpus
    # There are many different spellings of requestgpus, RequestGpus, RequestGpus
    # So eval, which is case insensitive (from my testing)
    try:
        r.GPUs(int(classad.eval('RequestGpus')), metric="max")
    except:
        # No GPUs, no problem
        # Or error converting to int, then just ignore GPUs
        pass

    if 'MyType' in classad:
        r.AdditionalInfo("CondorMyType", classad['MyType'])

    if 'AccountingGroup' in classad:
        r.AdditionalInfo("AccountingGroup", classad['AccountingGroup'])

    if 'ExitBySignal' in classad:
        if classad['ExitBySignal']:
            # Gratia expects lower-case; python produces "True".
            r.AdditionalInfo('ExitBySignal', 'true')
        else:
            r.AdditionalInfo('ExitBySignal', 'false')
    if 'ExitSignal' in classad:
        r.AdditionalInfo("ExitSignal", classad['ExitSignal'])
    if 'ExitCode' in classad:
        r.AdditionalInfo("ExitCode", classad['ExitCode'])
    if 'JobStatus' in classad:
        r.AdditionalInfo("condor.JobStatus", classad['JobStatus'])
    if 'GratiaJobOrigin' in classad:
        if classad['GratiaJobOrigin'] == "GRAM":
            r.Grid("OSG", "GratiaJobOrigin = GRAM")
        else:
            r.Grid("Local", "GratiaJobOrigin not GRAM")

    resource_name = get_classad_resource_name(classad)
    if resource_name is not None:
        if campus_factory_usage.search(resource_name):
            r.Grid("Campus", "Campus Factory Usage")
        elif campus_flock_usage.search(resource_name):
            r.Grid("Campus", "Campus Flocking Usage")
        elif resource_name == "Local Job":
            r.Grid("Local", "Local execution based on ResourceName")

    if 'JobUniverse' in classad:
        # scheduler and local universes are always considered to be local
        if classad['JobUniverse'] == 7 or classad['JobUniverse'] == 12:
            r.Grid("Local", "Local execution based on job universe")

    # Jobs that are routed to other jobs should be considered local
    if 'RoutedToJobId' in classad:
        r.Grid("Local", "Source of routed job")

    if 'logfile' in classad:
        r.AddTransientInputFile(classad["logfile"])

    setIfExists(r.ProjectName, classad, 'ProjectName', 'As specified in Condor submit file', True)

    if 'GratiaSiteName' in classad and hasattr(classad, 'lookup'):
        evaluated = classad.lookup('GratiaSiteName').eval()
        if isinstance(evaluated, bytes):
            r.SiteName(evaluated)
            r.ProbeName('%s-%s' % (r.GetProbeName(), evaluated))

    networkPhaseUnit = classad.get('RemoteWallClockTime', '')
    total_network = 0
    for attr, val in classad.items():
        if attr.startswith("Network"):
            total_network += val
            r.Network(val, storageUnit='b', phaseUnit=networkPhaseUnit, metric=attr, description=attr)
    r.Network(total_network, storageUnit='b', phaseUnit=networkPhaseUnit, metric="total")

    if not setIfExists(r.ExecutePool, classad, 'LastRemotePool', "Pool Host"):
        for host in get_collector_host_names():
            r.ExecutePool(host, "Pool Host from COLLECTOR_HOST")

    # Additional Pegasus attributes
    if 'pegasus_root_wf_uuid' in classad:
        r.AdditionalInfo("PegasusRootWFUUID", classad['pegasus_root_wf_uuid'])
    if 'pegasus_wf_uuid' in classad:
        r.AdditionalInfo("PegasusWFUUID", classad['pegasus_wf_uuid'])
    if 'pegasus_version' in classad:
        r.AdditionalInfo("PegasusVersion", classad['pegasus_version'])
    if 'pegasus_wf_app' in classad:
        r.AdditionalInfo("PegasusApp", classad['pegasus_wf_app'])
    if 'pegasus_wf_xformation' in classad:
        r.AdditionalInfo("PegasusWFXformation", classad['pegasus_wf_xformation'])

    return r

classad_bool_re = re.compile("^(\w{1,255}) = (true|True|TRUE|false|False|FALSE)$")
classad_int_re = re.compile("^(\w{1,255}) = (-?\d{1,30})$")
classad_double_re = re.compile("^(\w{1,255}) = ([+-]? *(?:\d{1,30}\.?\d{0,30}|\.\d{1,30})(?:[Ee][+-]?\d{1,30})?)$")
classad_string_re = re.compile("^(\S+) = \"(.*)\"$")
classad_catchall_re = re.compile("^(\S+) = (.*)$")
def fd_to_classad(fd):
    buffer = ''
    for lineOrig in fd:
        line = lineOrig.strip()
        if not line:
            yield add_unique_id(classadLib.parseOne(buffer))
            buffer = ''
        else:
            buffer += lineOrig

    yield add_unique_id(classadLib.parseOne(buffer))

def add_unique_id(classad):
    if 'GlobalJobId' in classad:
        classad['UniqGlobalJobId'] = 'condor.%s' % classad['GlobalJobId']
        DebugPrint(6, "Unique ID: %s" % classad['UniqGlobalJobId'])
    return classad

def setup_environment():
    """
    Make sure environment variables for HTCondor are in place   
    """ 
    try:
        condor_location = GratiaCore.Config.getConfigAttribute("CondorLocation")
        os.environ['CONDOR_LOCATION'] = condor_location        
        condor_config = GratiaCore.Config.getConfigAttribute("CondorConfig")
        if condor_config != '':
            os.environ['CONDOR_CONFIG'] = condor_config
    except:
        DebugPrint(0, "Can't setup CONDOR_LOCATION and CONDOR_CONFIG, exiting")
        return False
        
def htcondor_configured():
    """
    Make sure HTCondor is configured correctly for Gratia.   
    """ 
    try:
        path = GratiaCore.Config.getConfigAttribute("CondorLocation")
        if g_probe_config is not None and 'htcondor-ce' in g_probe_config:
          condor_config_binary = 'condor_ce_config_val'
        else:
          condor_config_binary = 'condor_config_val'
        condor_config_path = os.path.join(path, "bin", condor_config_binary)

        condor_config_val_args=['-schedd']
        schedd_name = GratiaCore.Config.getConfigAttribute('CondorScheddName')
        if schedd_name:
            condor_config_val_args.extend(['-name', schedd_name])
        condor_config_val_args.append('PER_JOB_HISTORY_DIR')

        if (os.path.exists(condor_config_path) and 
            os.path.isfile(condor_config_path)):
            args = [condor_config_path] + condor_config_val_args
            DebugPrint(4, 'Running command to check condor config: ' \
                          + ' '.join(args))
            cmd = subprocess.Popen(args, stdout=subprocess.PIPE)
        else:
            # args needs to be string when shell=True
            args = condor_config_binary + ' ' + ' '.join(condor_config_val_args)
            DebugPrint(4, 'Running command to check condor config: ' + args)
            cmd = subprocess.Popen(args, stdout=subprocess.PIPE, shell=True)
        cmd_stdout = utils.bytes2str(cmd.communicate()[0])
        # cmd_stdout contains the directory path, removing spaces
        # It will always be a string even if returncode != 0 
        cmd_stdout = cmd_stdout.strip()
        if cmd.returncode != 0:
            DebugPrint(-1, "WARNING: condor_config_val returned a non-zero " \
                           "return code. Maybe the schedd is overloaded.")
            return False
        elif 'Not defined' in cmd_stdout:
            DebugPrint(-1, "WARNING: PER_JOB_HISTORY_DIR not set in the default condor schedd " \
                           "config. You may need to change or reload the condor configuration " \
                           "or specify a different CondorScheddName in the Gratia config.")
            return False
        elif (not os.path.exists(cmd_stdout) or 
              not os.path.isdir(cmd_stdout)):
            DebugPrint(-1 , "WARNING: PER_JOB_HISTORY_DIR points to a " \
                            "non-existent or invalid directory: " \
                            "%s" % cmd_stdout)
            return False
        data_folder = GratiaCore.Config.getConfigAttribute('DataFolder')
        if not os.path.samefile(cmd_stdout, data_folder):
            DebugPrint(-1, "WARNING: PER_JOB_HISTORY_DIR (%s) and DataFolder "
                           "setting (%s) do not match!" %(cmd_stdout, data_folder))
            return False
        return True
    except subprocess.CalledProcessError:
        DebugPrint(-1, "WARNING: Can't get information on PER_JOB_HISTORY_DIR, " \
                       "condor_config_val returned non-zero exit code")
    return False


def get_collector_host():
    """
    Query for COLLECTOR_HOST
    """
    try:
        path = GratiaCore.Config.getConfigAttribute("CondorLocation")
        if g_probe_config is not None and 'htcondor-ce' in g_probe_config:
          #condor_config_binary = 'condor_ce_config_val'
          return None
        else:
          condor_config_binary = 'condor_config_val'
        condor_config_path = os.path.join(path, "bin", condor_config_binary)
        condor_config_val_args = ['COLLECTOR_HOST']

        if (os.path.exists(condor_config_path) and
            os.path.isfile(condor_config_path)):
            args = [condor_config_path] + condor_config_val_args
            cmd = subprocess.Popen(args, stdout=subprocess.PIPE)
        else:
            # args needs to be string when shell=True
            args = condor_config_binary + ' ' + ' '.join(condor_config_val_args)
            cmd = subprocess.Popen(args, stdout=subprocess.PIPE, shell=True)

        cmd_stdout = utils.bytes2str(cmd.communicate()[0])
        cmd_stdout = cmd_stdout.strip()
        if cmd.returncode != 0:
            DebugPrint(-1, "WARNING: condor_config_val COLLECTOR_HOST "
                           "returned a non-zero return code. Maybe the "
                           "condor_master is overloaded.")
            return None
        elif 'Not defined' in cmd_stdout:
            DebugPrint(-1, "WARNING: COLLECTOR_HOST not set in the default "
                           "condor master config. You may need to change or "
                           "reload the condor configuration.")
            return None

        return cmd_stdout
    except subprocess.CalledProcessError:
        DebugPrint(-1, "WARNING: Can't get information on COLLECTOR_HOST, "
                       "condor_config_val returned non-zero exit code")
        return None


def get_collector_host_names() -> List[str]:
    """
    Parse one or more host name values from get_collector_host()
    """
    collector_host = get_collector_host()
    hosts = []
    if not collector_host:
        return hosts
    for host in re.split(r'[ ,]+', collector_host.strip()):
        if host.startswith("<"):
            # Looks like `host` is not a host, it is a <sinful> string.
            # Parse alias out of it to get the actual host:port
            m = re.search(r'&alias=([^&>]+)', host)
            if m:
                host = m.group(1)
            else:
                continue
        if ':' in host:  # `host` is a host:port but we just want the host
            hosts.append(host.split(':')[0])
        else:
            hosts.append(host)
    return hosts


def send_alternate_records(gratia_info):
    """
    For any accumulated records with an alternate probe/site name, send in a sub-process.
    """
    if not gratia_info:
        return
    GratiaCore.Disconnect()
    for info, records in gratia_info.items():
        pid = os.fork()
        if pid == 0: # I am the child
            try:
                signal.alarm(5*60)
                send_alternate_records_child(info, records)
            except Exception as e:
                DebugPrint(2, "Failed to send alternate records: %s" % str(e))
                DebugPrintTraceback(2)
                os._exit(0)
            os._exit(0)
        else: # I am parent
            try:
                os.waitpid(pid, 0)
            except:
                raise


def send_alternate_records_child(info, record_list):
    probe, site = info

    try:
        GratiaCore.Initialize(g_probe_config)
    except Exception as e:
        DebugPrint(2, "Failed to send alternate records: %s" % str(e))
        DebugPrintTraceback(2)
        raise
    config.Config.setSiteName(site)
    config.Config.setMeterName(probe)
    GratiaCore.Handshake()
    try:
        GratiaCore.SearchOutstandingRecord()
    except Exception as e:
        DebugPrint(2, "Failed to send alternate records: %s" % str(e)) 
        DebugPrintTraceback(2)
        raise
    GratiaCore.Reprocess()

    DebugPrint(2, "Sending alternate records for probe %s / site %s." % (probe, site))
    DebugPrint(2, "Gratia collector to use: %s" % GratiaCore.Config.get_SOAPHost())

    count_found = 0
    count_submit = 0
    for record in record_list:
        count_found += 1
        record.ProbeName(probe)
        record.SiteName(site)
        response = GratiaCore.Send(record)
        if response[:2] == 'OK':
            count_submit += 1
        DebugPrint(4, "Sending record for probe %s in site %s to Gratia: %s."% \
            (probe, site, response))

    DebugPrint(2, "Number of usage records submitted: %d" % count_submit)
    DebugPrint(2, "Number of usage records found: %d" % count_found)

    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        DebugPrint(-1, "ERROR: Unexpected error encountered: %s" % e)
        DebugPrintTraceback()
        raise
