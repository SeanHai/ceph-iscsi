#!/usr/bin/python -u
# NB the python environment is using unbuffered mode (-u), so any "print" statements will appear in the
# syslog 'immediately'

import signal
import logging
import logging.handlers
import netifaces
import subprocess
import time
import sys
import os
import rados
import rbd


from rtslib_fb import root

import ceph_iscsi_config.settings as settings

from ceph_iscsi_config.gateway import GWTarget
from ceph_iscsi_config.lun import LUN
from ceph_iscsi_config.client import GWClient, CHAP
from ceph_iscsi_config.common import Config
from ceph_iscsi_config.lio import LIO, Gateway
from ceph_iscsi_config.utils import this_host, human_size


def ceph_rm_blacklist(blacklisted_ip):
    """
    Issue a ceph osd blacklist rm command for a given IP on this host
    :param blacklisted_ip: IP address (str - dotted quad)
    :return: boolean for success of the rm operation
    """

    logger.info("Removing blacklisted entry for this host : "
                "{}".format(blacklisted_ip))

    result = subprocess.check_output("ceph osd blacklist rm {}".
                                     format(blacklisted_ip),
                                     stderr=subprocess.STDOUT, shell=True)
    if "un-blacklisting" in result:
        logger.info("Successfully removed blacklist entry")
        return True
    else:
        logger.critical("blacklist removal failed. Run"
                        " 'ceph osd blacklist rm {}'".format(blacklisted_ip))
        return False


def signal_stop(*args):
    """
    Handler to shutdown the service when systemd sends SIGTERM
    NB - args has to be specified since python will pass two parms into the
    handler by default
    :param args: ignored/unused
    """
    logger.info("rbd-target-gw stop received, refreshing local state")
    config.refresh()
    if config.error:
        logger.critical("Problems accessing config object"
                        " - {}".format(config.error_msg))
        sys.exit(16)

    local_gw = this_host()

    if "gateways" in config.config:
        if local_gw not in config.config["gateways"]:
            logger.info("No gateway configuration to remove on this host "
                        "({})".format(local_gw))
            sys.exit(0)
    else:
        logger.info("Configuration object does not hold any gateway metadata"
                    " - nothing to do")
        sys.exit(0)

    # At this point, we're working with a config object that has an entry for
    # this host

    lio = LIO()
    gw = Gateway(config)

    # This will fail incoming IO, but wait on outstanding IO to
    # complete normally. We rely on the initiator multipath layer
    # to handle retries like a normal path failure.
    logger.debug("Removing iSCSI target from LIO")
    gw.drop_target(local_gw)
    if gw.error:
        logger.error("rbd-target-gw failed to remove target objects")

    logger.debug("Removing LUNs from LIO")
    lio.drop_lun_maps(config, False)
    if lio.error:
        logger.error("rbd-target-gw failed to remove lun objects")

    logger.info("Active gateway configuration removed")
    sys.exit(0)


def signal_reload(*args):
    """
    Handler to invoke an refresh of the config, when systemd issues a SIGHUP
    NB - args has to be specified since python will pass two parms into the
    handler by default
    :param args: unused
    :return: runs the apply_config function
    """
    if not config_loading:
        logger.info("Reloading configuration from rados configuration object")

        config.refresh()
        if config.error:
            halt("Unable to read the configuration object - "
                 "{}".format(config.error_msg))

        apply_config()

    else:
        logger.warning("Admin attempted to reload the config during an active "
                       "reload process - skipped, try later")


def osd_blacklist_cleanup():
    """
    Process the osd's to see if there are any blacklist entries for this node
    :return: True, blacklist entries removed OK, False - problems removing
    a blacklist
    """

    logger.info("Processing osd blacklist entries for this node")

    cleanup_state = True

    try:

        # NB. Need to use the stderr override to catch the output from
        # the command
        blacklist = subprocess.check_output("ceph osd blacklist ls",
                                            shell=True,
                                            stderr=subprocess.STDOUT)

    except subprocess.CalledProcessError:
        logger.critical("Failed to run 'ceph osd blacklist ls'. "
                        "Please resolve manually...")
        cleanup_state = False
    else:

        blacklist_output = blacklist.split('\n')[:-1]
        if len(blacklist_output) > 1:

            # We have entries to look for, so first build a list of ipv4
            # addresses on this node
            ipv4_list = []
            for iface in netifaces.interfaces():
                dev_info = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                ipv4_list += [dev['addr'] for dev in dev_info]

            # process the entries (first entry just says "Listed X entries,
            # last entry is just null)
            for blacklist_entry in blacklist_output[1:]:

                # valid entries to process look like -
                # 192.168.122.101:0/3258528596 2016-09-28 18:23:15.307227
                blacklisted_ip = blacklist_entry.split(':')[0]
                # Look for this hosts ipv4 address in the blacklist

                if blacklisted_ip in ipv4_list:
                    # pass in the ip:port/nonce
                    rm_ok = ceph_rm_blacklist(blacklist_entry.split(' ')[0])
                    if not rm_ok:
                        cleanup_state = False
                        break
        else:
            logger.info("No OSD blacklist entries found")

    return cleanup_state


def halt(message):
    logger.critical(message)
    sys.exit(16)


def get_tpgs():
    """
    determine the number of tpgs in the current LIO environment
    :return: count of the defined tpgs
    """

    return len([tpg.tag for tpg in root.RTSRoot().tpgs])


def apply_config():
    """
    main procesing logic that takes the config object from rados and applies
    it to the local LIO instance using the config module classes (also used by
    the ansible playbooks)
    :return: return code 0 = all is OK, anything
    """

    # access config_loading from the outer scope, for r/w
    global config_loading
    config_loading = True

    local_gw = this_host()

    logger.info("Reading the configuration object to update local LIO "
                "configuration")

    logger.info("Processing Gateway configuration")

    # first check to see if we have any entries to handle - if not, there is
    # no work to do..
    if "gateways" not in config.config:
        logger.info("Configuration is empty - nothing to define to LIO")
        config_loading = False
        return
    if local_gw not in config.config['gateways']:
        logger.info("Configuration does not have an entry for this host({}) - "
                    "nothing to define to LIO".format(local_gw))
        config_loading = False
        return

    # at this point we have a gateway entry that applies to the running host

    gw_ip_list = config.config['gateways']['ip_list'] if 'ip_list' in config.config['gateways'] else None
    gw_iqn = config.config['gateways']['iqn'] if 'iqn' in config.config['gateways'] else None
    gw_nodes = [key for key in config.config['gateways'] if isinstance(config.config['gateways'][key], dict)]

    # Gateway Definition : Handle the creation of the Target/TPG(s) and Portals
    # Although we create the tpgs, we flick the enable_portal flag off so the
    # enabled tpg will not have an outside IP address. This prevents clients
    # from logging in too early, failing and giving up because the nodeACL
    # hasn't been defined yet (yes Windows I'm looking at you!)

    # first check if there are tpgs already in LIO (True) - this would indicate
    # a restart or reload call has been made. If the tpg count is 0, this is a
    # boot time request
    portals_active = get_tpgs() > 0

    gateway = GWTarget(logger,
                       gw_iqn,
                       gw_ip_list,
                       enable_portal=portals_active)

    gateway.manage('target')
    if gateway.error:
        halt("Error creating the iSCSI target (target, TPGs, Portals)")

    logger.info("Processing LUN configuration")
    # sort the disks dict keys, so the disks are registered in a specific
    # sequence
    disks = config.config['disks']
    srtd_disks = sorted(disks)
    pools = {disks[disk_key]['pool'] for disk_key in srtd_disks}

    if pools:
        with rados.Rados(conffile=settings.config.cephconf) as cluster:

            for pool in pools:

                logger.debug("Processing rbd's in '{}' pool".format(pool))

                with cluster.open_ioctx(pool) as ioctx:

                    pool_disks = [disk_key for disk_key in srtd_disks
                                  if disk_key.startswith(pool)]
                    for disk_key in pool_disks:

                        pool, image_name = disk_key.split('.')

                        with rbd.Image(ioctx, image_name) as rbd_image:
                            image_bytes = rbd_image.size()
                            image_size_h = human_size(image_bytes)

                            lun = LUN(logger, pool, image_name,
                                      image_size_h, local_gw)
                            if lun.error:
                                halt("Error defining rbd image {}".format(
                                    disk_key))

                            lun.allocate()
                            if lun.error:
                                halt("Error unable to register {} with LIO - "
                                     "{}".format(disk_key, lun.error_msg))

        # Gateway Mapping : Map the LUN's registered to all tpg's within the
        # LIO target
        gateway.manage('map')
        if gateway.error:
            halt("Error mapping the LUNs to the tpg's within the iscsi Target")

    else:
        logger.info("No LUNs to export")

    logger.info("Processing client configuration")

    # Client configurations (NodeACL's)
    for client_iqn in config.config['clients']:
        client_metadata = config.config['clients'][client_iqn]
        client_chap = CHAP(client_metadata['auth']['chap'])

        image_list = client_metadata['luns'].keys()

        chap_str = client_chap.chap_str
        if client_chap.error:
            logger.debug("Password decode issue : {}".format(client_chap.error_msg))
            halt("Unable to decode password for {}".format(client_iqn))

        client = GWClient(logger, client_iqn, image_list, client_chap.chap_str)
        client.manage('present')  # ensure the client exists

    if not portals_active:
        # The tpgs, luns and clients are all defined, but the active tpg
        # doesn't have an IP bound to it yet (due to the enable_portals=False
        # setting above)
        logger.info("Adding the IP to the enabled tpg, allowing iSCSI logins")
        gateway.enable_active_tpg(config)
        if gateway.error:
            halt("Error enabling the IP with the active TPG")

    config_loading = False

    logger.info("iSCSI configuration load complete")


def main():

    # only look for osd blacklist entries when the service starts
    osd_state_ok = osd_blacklist_cleanup()
    if not osd_state_ok:
        sys.exit(16)

    # Read the configuration object and apply to the local LIO instance
    if not config_loading:
        apply_config()

    # Just keep the main process alive to receive SIGHUP/SIGTERM
    while True:
        time.sleep(1)


if __name__ == '__main__':

    # Setup signal handlers for stop and reload actions from systemd
    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGHUP, signal_reload)

    # setup syslog handler to help diagnostics
    logger = logging.getLogger('rbd-target-gw')
    logger.setLevel(logging.DEBUG)

    # syslog (systemctl/journalctl messages)
    syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
    syslog_handler.setLevel(logging.INFO)
    syslog_format = logging.Formatter("%(message)s")
    syslog_handler.setFormatter(syslog_format)

    # file target - more verbose logging for diagnostics
    file_handler = logging.FileHandler('/var/log/rbd-target-gw.log', mode='w')
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter("%(asctime)s [%(levelname)8s] - %(message)s")
    file_handler.setFormatter(file_format)

    logger.addHandler(syslog_handler)
    logger.addHandler(file_handler)

    # config_loading is defined in the outer-scope allowing it to be used as
    # a flag to indicate when the apply_config function is running to prevent
    # multiple reloads from being triggered concurrently
    config_loading = False

    settings.init()

    # config is set in the outer scope, so it's easily accessible to the
    # api classes
    config = Config(logger)
    if config.error:
        halt("Unable to open/read the configuration object")
    else:
        main()
