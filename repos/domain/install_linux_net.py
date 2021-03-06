#! /usr/bin/env python
# Install a linux domain from network

import os
import sys
import re
import time
import commands
import shutil
import urllib

import libvirt
from libvirt import libvirtError

from src import sharedmod
from src import env_parser
from utils import utils

required_params = ('guestname', 'guestos', 'guestarch',)
optional_params = {'memory': 1048576,
                   'vcpu': 1,
                   'disksize': 10,
                   'diskpath': '/var/lib/libvirt/images/libvirt-test-api',
                   'imageformat': 'raw',
                   'hddriver': 'virtio',
                   'nicdriver': 'virtio',
                   'macaddr': '52:54:00:97:e4:28',
                   'uuid': '05867c1a-afeb-300e-e55e-2673391ae080',
                   'netmethod': 'http',
                   'type': 'define',
                   'xml': 'xmls/kvm_linux_guest_install_net.xml',
                   'guestmachine': 'pc',
                   }

VIRSH_QUIET_LIST = "virsh --quiet list --all|awk '{print $2}'|grep \"^%s$\""
VM_STAT = "virsh --quiet list --all| grep \"\\b%s\\b\"|grep off"
VM_DESTROY = "virsh destroy %s"
VM_UNDEFINE = "virsh undefine %s"

BOOT_DIR = "/var/lib/libvirt/boot"
VMLINUZ = os.path.join(BOOT_DIR, 'vmlinuz')
INITRD = os.path.join(BOOT_DIR, 'initrd.img')
HOME_PATH = os.getcwd()


def prepare_boot_guest(domobj, xmlstr, guestname, logger, installtype):
    """After guest installation is over, undefine the guest with
       bootting off cdrom, to define the guest to boot off harddisk.
    """

    xmlstr = re.sub("<kernel>.*</kernel>\n", "", xmlstr)
    xmlstr = re.sub("<initrd>.*</initrd>\n", "", xmlstr)
    xmlstr = re.sub("<cmdline>.*</cmdline>\n", "", xmlstr)

    if installtype != 'create':
        domobj.undefine()
        logger.info("undefine %s : \n" % guestname)

    try:
        conn = domobj._conn
        domobj = conn.defineXML(xmlstr)
    except libvirtError as e:
        logger.error("API error message: %s, error code is %s"
                     % (e.message, e.get_error_code()))
        logger.error("fail to define domain %s" % guestname)
        return 1

    logger.info("define guest %s " % guestname)
    logger.debug("the xml description of guest booting off harddisk is %s" %
                 xmlstr)

    logger.info('boot guest up ...')

    try:
        domobj.create()
    except libvirtError as e:
        logger.error("API error message: %s, error code is %s"
                     % (e.message, e.get_error_code()))
        logger.error("fail to start domain %s" % guestname)
        return 1

    return 0


def check_domain_state(conn, guestname, logger):
    """ if a guest with the same name exists, remove it """
    running_guests = []
    ids = conn.listDomainsID()
    for id in ids:
        obj = conn.lookupByID(id)
        running_guests.append(obj.name())

    if guestname in running_guests:
        logger.info("A guest with the same name %s is running!" % guestname)
        logger.info("destroy it...")
        domobj = conn.lookupByName(guestname)
        domobj.destroy()

    defined_guests = conn.listDefinedDomains()

    if guestname in defined_guests:
        logger.info("undefine the guest with the same name %s" % guestname)
        domobj = conn.lookupByName(guestname)
        domobj.undefine()


def install_linux_net(params):
    """install a new virtual machine"""
    # Initiate and check parameters
    logger = params['logger']

    guestname = params.get('guestname')
    guestos = params.get('guestos')
    guestarch = params.get('guestarch')
    xmlstr = params['xml']

    installmethod = params.get('netmethod', 'http')

    logger.info("the name of guest is %s" % guestname)
    logger.info("the installation method is %s" % installmethod)

    conn = sharedmod.libvirtobj['conn']
    check_domain_state(conn, guestname, logger)

    logger.info("the macaddress is %s" %
                params.get('macaddr', '52:54:00:97:e4:28'))

    diskpath = params.get(
        'diskpath',
        '/var/lib/libvirt/images/libvirt-test-api')
    logger.info("disk image is %s" % diskpath)
    seeksize = params.get('disksize', 10)
    imageformat = params.get('imageformat', 'raw')
    logger.info(
        "create disk image with size %sG, format %s" %
        (seeksize, imageformat))
    disk_create = "qemu-img create -f %s %s %sG" % \
        (imageformat, diskpath, seeksize)
    logger.debug("the command line of creating disk images is '%s'" %
                 disk_create)

    (status, message) = commands.getstatusoutput(disk_create)
    if status != 0:
        logger.debug(message)
        return 1

    os.chown(diskpath, 107, 107)
    logger.info("creating disk images file is successful.")

    hddriver = params.get('hddriver', 'virtio')
    if hddriver == 'virtio':
        xmlstr = xmlstr.replace('DEV', 'vda')
    elif hddriver == 'ide':
        xmlstr = xmlstr.replace('DEV', 'hda')
    elif hddriver == 'scsi':
        xmlstr = xmlstr.replace('DEV', 'sda')

    logger.info("get system environment information")
    envfile = os.path.join(HOME_PATH, 'global.cfg')
    logger.info("the environment file is %s" % envfile)

    envparser = env_parser.Envparser(envfile)

    # Get http, ftp or nfs url based on guest os, arch
    # and installation method from global.cfg

    os_arch = guestos + "_" + guestarch

    if installmethod == 'http':
        ks = envparser.get_value("guest", os_arch + "_http_ks")
    elif installmethod == 'ftp':
        ks = envparser.get_value("guest", os_arch + "_ftp_ks")
    elif installmethod == "nfs":
        ks = envparser.get_value("guest", os_arch + "_nfs_ks")

    xmlstr = xmlstr.replace('KS', ks)

    ostree = envparser.get_value("guest", os_arch)
    logger.debug('install source:\n    %s' % ostree)
    logger.debug('kisckstart file:\n    %s' % ks)

    if (ostree == 'http://'):
        logger.error("no os tree defined in %s for %s" % (envfile, os_arch))
        return 1

    logger.info('prepare installation...')

    vmlinuzpath = os.path.join(ostree, 'isolinux/vmlinuz')
    initrdpath = os.path.join(ostree, 'isolinux/initrd.img')

    logger.debug("the url of vmlinuz file is %s" % vmlinuzpath)
    logger.debug("the url of initrd file is %s" % initrdpath)

    urllib.urlretrieve(vmlinuzpath, VMLINUZ)
    urllib.urlretrieve(initrdpath, INITRD)

    logger.debug("vmlinuz and initrd.img are located in %s" % BOOT_DIR)

    xmlstr = xmlstr.replace('KERNEL', VMLINUZ)
    xmlstr = xmlstr.replace('INITRD', INITRD)
    logger.debug('dump installation guest xml:\n%s' % xmlstr)

    installtype = params.get('type', 'define')
    if installtype == 'define':
        logger.info('define guest from xml description')
        try:
            domobj = conn.defineXML(xmlstr)
        except libvirtError as e:
            logger.error("API error message: %s, error code is %s"
                         % (e.message, e.get_error_code()))
            logger.error("fail to define domain %s" % guestname)
            return 1

        logger.info('start installation guest ...')

        try:
            domobj.create()
        except libvirtError as e:
            logger.error("API error message: %s, error code is %s"
                         % (e.message, e.get_error_code()))
            logger.error("fail to start domain %s" % guestname)
            return 1
    elif installtype == 'create':
        logger.info('create guest from xml description')
        try:
            domobj = conn.createXML(xmlstr, 0)
        except libvirtError as e:
            logger.error("API error message: %s, error code is %s"
                         % (e.message, e.get_error_code()))
            logger.error("fail to define domain %s" % guestname)
            return 1

    if 'rhel3u9' in guestos:
        interval = 0
        logger.info("waiting 1000 seconds for the installation to complete...")
        while(interval < 1000):
            logger.info('%s seconds passed away...' % interval)
            time.sleep(10)
            interval += 10

        domobj.destroy()
        ret = prepare_boot_guest(
            domobj, xmlstr, guestname, logger, installtype)

        if ret:
            logger.info("booting guest vm off harddisk failed")
            return 1
        else:
            logger.info("guest is booting up")
    else:
        interval = 0
        while(interval < 3600):
            time.sleep(10)
            if installtype is None or installtype == 'define':
                state = domobj.info()[0]
                if(state == libvirt.VIR_DOMAIN_SHUTOFF):
                    logger.info("guest installaton of define type is complete")
                    logger.info("boot guest vm off harddisk")
                    ret = prepare_boot_guest(domobj, xmlstr, guestname, logger,
                                             installtype)
                    if ret:
                        logger.info("booting guest vm off harddisk failed")
                        return 1
                    break
                else:
                    interval += 10
                    logger.info('%s seconds passed away...' % interval)
            elif installtype == 'create':
                guest_names = []
                ids = conn.listDomainsID()
                for id in ids:
                    obj = conn.lookupByID(id)
                    guest_names.append(obj.name())

                if guestname not in guest_names:
                    logger.info(
                        "guest installation of create type is complete")
                    logger.info("define the vm and boot it up")
                    ret = prepare_boot_guest(domobj, xmlstr, guestname, logger,
                                             installtype)
                    if ret:
                        logger.info("booting guest vm off harddisk failed")
                        return 1
                    break
                else:
                    interval += 10
                    logger.info('%s seconds passed away...' % interval)

        if interval == 3600:
            logger.info("guest installation timeout 3600s")
            return 1
        else:
            logger.info("guest is booting up")

    logger.info("get the mac address of vm %s" % guestname)
    mac = utils.get_dom_mac_addr(guestname)
    logger.info("the mac address of vm %s is %s" % (guestname, mac))

    timeout = 300
    while timeout:
        time.sleep(10)
        timeout -= 10

        ip = utils.mac_to_ip(mac, 180)

        if not ip:
            logger.info(str(timeout) + "s left")
        else:
            logger.info("vm %s power on successfully" % guestname)
            logger.info("the ip address of vm %s is %s" % (guestname, ip))
            break

        if timeout == 0:
            logger.info("fail to power on vm %s" % guestname)
            return 1

    return 0


def install_linux_net_clean(params):
    """ clean testing environment """
    logger = params['logger']
    guestname = params.get('guestname')

    diskpath = params.get(
        'diskpath',
        '/var/lib/libvirt/images/libvirt-test-api')

    (status, output) = commands.getstatusoutput(VIRSH_QUIET_LIST % guestname)
    if not status:
        logger.info("remove guest %s, and its disk image file" % guestname)
        (status, output) = commands.getstatusoutput(VM_STAT % guestname)
        if status:
            (status, output) = commands.getstatusoutput(VM_DESTROY % guestname)
            if status:
                logger.error("failed to destroy guest %s" % guestname)
                logger.error("%s" % output)
            else:
                (status, output) = commands.getstatusoutput(
                    VM_UNDEFINE % guestname)
                if status:
                    logger.error("failed to undefine guest %s" % guestname)
                    logger.error("%s" % output)
        else:
            (status, output) = commands.getstatusoutput(VM_UNDEFINE % guestname)
            if status:
                logger.error("failed to undefine guest %s" % guestname)
                logger.error("%s" % output)

    if os.path.exists(diskpath):
        os.remove(diskpath)

    if os.path.exists(VMLINUZ):
        os.remove(VMLINUZ)
    if os.path.exists(INITRD):
        os.remove(INITRD)
