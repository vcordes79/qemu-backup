#!/usr/bin/env python3

from __future__ import print_function
import libvirt
import sys
import shutil
import re
import time
import argparse
import subprocess
import os
import fcntl
from pathlib import Path
import xml.etree.ElementTree as ET

archive_info = { }
omit_unsafe = False

def lock_acquire(lpath):
  fd = None
  try:
    fd = os.open(lpath, os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_NB | fcntl.LOCK_EX)
    return True
  except (OSError, IOError):
    if fd: os.close(fd)
    return False

def check_backup_chain(domain, backupset, devs_to_check, args):
    if not domain in archive_info or not backupset in archive_info[domain]:
        return
#    print(archive_info[domain][backupset])
    for drive in devs_to_check:
        for interval in archive_info[domain][backupset][drive]['images']:
            if interval == 'daily' and 0 not in archive_info[domain][backupset][drive]['images'][interval]:
                num = 1
                while num in archive_info[domain][backupset][drive]['images'][interval]:
                    filename = archive_info[domain][backupset][drive]['images'][interval];
                    newfilename = filename.replace('.'+str(num)+'.img', '.'+str(num-1)+'.img')
                    os.rename(args.backup_dir + '/' + filename, args.backup_dir + '/' + newfilename)
                    archive_info[domain][backupset][drive]['images'][interval][num-1] = filename.replace('.'+str(num)+'.img', '.'+str(num-1)+'.img')
                    del archive_info[domain][backupset][drive]['images'][interval][num]
                    num = num + 1
            image_count = len(archive_info[domain][backupset][drive]['images'][interval])
            if interval != 'base' and image_count != max(archive_info[domain][backupset][drive]['images'][interval].keys()) + 1:
                raise Exception('Images missing in backup chain for interval ' + interval)

def vm_get_blockdevs(libvirt_conn, vm_name):
    try:
        vm = libvirt_conn.lookupByName(vm_name)
    except libvirt.libvirtError as e:
        # Error code 42 = Domain not found
        if (e.get_error_code() == 42):
            print(e)
            exit(1)
        else:
            raise(e)
    tree = ET.fromstring(vm.XMLDesc(0))

    blockdevs = {}
    for blockdev in tree.findall("devices/disk"):
        if blockdev.get("device") != "disk":
            continue
        target = blockdev.find("target")
        if target is None: continue
        dev = target.get("dev")
        file = blockdev.find("source").get("file")
        blockdevs[dev] = file
    return blockdevs

def get_backing_file(image):
    if omit_unsafe:
        info_output = subprocess.run(['qemu-img', 'info', image], stdout=subprocess.PIPE, universal_newlines=True)
    else:
        info_output = subprocess.run(['qemu-img', 'info', '-U', image], stdout=subprocess.PIPE, universal_newlines=True)
    if info_output.returncode != 0:
        raise Exception('Could not get info on file ' + image)
    info = {}
    for x in info_output.stdout.split('\n'):
        x = x.split(':')
        if len(x) > 1 and x[1] != '':
            info[x[0].strip()] = x[1].strip()

    if not 'backing file' in info:
        return ''

    p = Path(image)
    bf = info['backing file'].split(' ')
    bf_path = Path(bf[0])

    if "/" in bf[0] and p.parent.as_posix() != bf_path.parent.as_posix():
        img_rebase(image, p.parent.as_posix(), bf_path.name)

    return p.parent.as_posix() + '/' + bf_path.name

def get_snapshot_chain(image):
    snapshot_chain = [ image ]
    bf = get_backing_file(image)
    while bf != '':
        snapshot_chain.append(bf)
        bf = get_backing_file(bf)
    return snapshot_chain

def get_backup_chain(backup_dir, vm_name):
    chain = {}
    for image in backup_path.glob(vm_name + '*.img'):
        if not image.is_file():
            continue

        imgdata = image.name.split('.')
        # 0: domain name, 1: b<nr>, 2: <drive>, 3: inc<nr>[-<nr>] | base, 4: <interval>, 5: <nr>, 6: img
        if not imgdata[1] in chain:
            chain[imgdata[1]] = {}
        chain[imgdata[1]][image.name] = imgdata
#        print(imgdata)

    return chain

def img_rename(old_filename, new_filename):
    stat = os.stat(old_filename)
    os.rename(old_filename, new_filename)
    os.utime(new_filename, (stat.st_atime, stat.st_mtime))

def img_copy_to_backup_dir(filename, new_filename, args):
    new_path = Path(args.backup_dir + '/' + new_filename)
    if new_path.exists():
        raise ValueError(new_path.name + ' already exists in backup dir. Please clean up manually.')
    if args.compress:
        backing_file = get_backing_file(filename)
        if backing_file:
            info_output = subprocess.run(['qemu-img', 'convert', '-c', '-f', 'qcow2', '-O', 'qcow2', '-B', backing_file, filename, args.backup_dir+'/'+new_filename], stdout=subprocess.PIPE, universal_newlines=True)
        else:
            info_output = subprocess.run(['qemu-img', 'convert', '-c', '-f', 'qcow2', '-O', 'qcow2', filename, args.backup_dir+'/'+new_filename], stdout=subprocess.PIPE, universal_newlines=True)
        if info_output.returncode != 0:
            raise Exception('Error compressing ' + filename)
    elif args.copy:
        shutil.copy(filename, args.backup_dir+'/'+new_filename)
    else:
        backing_file = get_backing_file(filename)
        if backing_file:
            info_output = subprocess.run(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', '-B', backing_file, filename, args.backup_dir+'/'+new_filename], stdout=subprocess.PIPE, universal_newlines=True)
        else:
            info_output = subprocess.run(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', filename, args.backup_dir+'/'+new_filename], stdout=subprocess.PIPE, universal_newlines=True)
        if info_output.returncode != 0:
            raise Exception('Error converting ' + filename)

def img_rebase(image, backing_file_dir, new_backing_file):
    stat = os.stat(image)
    info_output = subprocess.run(['qemu-img', 'rebase', '-u', '-b', new_backing_file, image], stdout=subprocess.PIPE, universal_newlines=True, cwd=backing_file_dir)
    if info_output.returncode != 0:
        raise Exception('Error rebasing ' + image + ' on ' + new_backing_file)
    os.utime(image, (stat.st_atime, stat.st_mtime))

    # print ('rebase ' + image + ' on ' + new_backing_file)
    return

def img_rotate_interval(vm_name, backupset, interval, dev_to_commit, vm_info, args):
    # print(archive_info[vm_name]["b%03d" % (backupset)][dev_to_commit])
    bs = "b%03d" % (backupset)
    interval_name = args.intervals[interval][0]
    if not interval_name in archive_info[vm_name][bs][dev_to_commit]['images']:
        return # no images yet
    images = archive_info[vm_name][bs][dev_to_commit]['images'][interval_name]

    if len(images) >= args.intervals[interval][1]:
        base = max(images.keys())
        top = args.intervals[interval][1]-2
        baseimage = images[base]
        topimage = images[top]
        stat = os.stat(args.backup_dir + '/' + topimage)
        info_output = subprocess.run(['qemu-img', 'commit', '-b', args.backup_dir + '/' + baseimage, args.backup_dir + '/' + topimage], stdout=subprocess.PIPE, universal_newlines=True)
        if info_output.returncode != 0:
            raise Exception('Error commiting ' + images[top] + ' into ' + images[base])
        os.utime(args.backup_dir + '/' + baseimage, (stat.st_atime, stat.st_mtime))
        # commit oldest images
        for i in range(top, base):
            os.unlink(args.backup_dir + '/' + images[i])
            del images[i]
        del images[base]
        imgdata_base = baseimage.split('.')
        imgdata_top = topimage.split('.')
        imgdata_base[3] = imgdata_base[3].split('-')
        if len(imgdata_base[3]):
            imgdata_base[3].append(imgdata_base[3][0])
        imgdata_top[3] = imgdata_top[3].split('-')
        imgdata_base[3][1] = imgdata_top[3][0 if (len(imgdata_top[3]) == 1) else 1]
        imgdata_base[3] = imgdata_base[3][0] + '-' + imgdata_base[3][1]
        imgdata_base[5] = "%d" % (top)
        new_name = '.'.join(imgdata_base)
        img_rename(args.backup_dir + '/' + baseimage, args.backup_dir + '/' + new_name)
        images[top] = new_name
        img_rebase(args.backup_dir + '/' + images[top-1], args.backup_dir, new_name)

    for i in range(len(images)-1, -1, -1):
        old_filename = images[i]
        new_filename = old_filename.replace("%s.%d" % (interval_name, i), "%s.%d" % (interval_name, i+1))
        img_rename(args.backup_dir + '/' + old_filename, args.backup_dir + '/' + new_filename)
        if i != len(images) - 1:
            img_rebase(args.backup_dir + '/' + new_filename, args.backup_dir, images[i+1].replace("%s.%d" % (interval_name, i+1), "%s.%d" % (interval_name, i+2)))
        images[i+1] = new_filename

    del images[0]

def vm_commit_first(libvirt_conn, vm_name, vm_info, devs_to_commit, args):
    for dev in devs_to_commit:
        if len(vm_info[dev]['chain']) > 1:
            info_output = subprocess.run(['virsh', 'blockcommit', vm_name, dev, '--wait', '--top' , vm_info[dev]['chain'][-2]], stdout=subprocess.PIPE, universal_newlines=True)
            if info_output.returncode != 0:
                raise Exception('Error commiting changes for ' + dev + ' of ' + vm_name)

    blockdevs = vm_get_blockdevs(libvirt_conn, vm_name)
    for dev in devs_to_commit:
        chain = get_snapshot_chain(blockdevs[dev])
        if len(chain) != 2:
            raise Exception('There are still block devices with other than two image files')
        for img in vm_info[dev]['chain']:
            if not img in chain:
                os.unlink(img)
        vm_info[dev]['chain'] = chain

def vm_commit_all(libvirt_conn, vm_name, vm_info, devs_to_commit, args):
    try:
        vm = libvirt_conn.lookupByName(vm_name)
    except libvirt.libvirtError as e:
        # Error code 42 = Domain not found
        if (e.get_error_code() == 42):
            print(e)
            exit(1)
        else:
            raise(e)

    for dev in devs_to_commit:
        if len(vm_info[dev]['chain']) > 1:
            info_output = subprocess.run(['virsh', 'blockcommit', vm_name, dev, '--active', '--wait', '--pivot'], stdout=subprocess.PIPE, universal_newlines=True)
            if info_output.returncode != 0:
                raise Exception('Error commiting changes for ' + dev + ' of ' + vm_name)

    blockdevs = vm_get_blockdevs(libvirt_conn, vm_name)
    for dev in devs_to_commit:
        chain = get_snapshot_chain(blockdevs[dev])
        if len(chain) > 1:
            raise Exception('There are still block devices with more than one image file')
        for img in vm_info[dev]['chain']:
            if img != chain[0]:
                os.unlink(img)
        vm_info[dev]['chain'] = chain

def vm_trim(libvirt_conn, vm_name):
    try:
        vm = libvirt_conn.lookupByName(vm_name)
    except libvirt.libvirtError as e:
        # Error code 42 = Domain not found
        if (e.get_error_code() == 42):
            print(e)
            exit(1)
        else:
            raise(e)
    try:
        vm.fSTrim(None, 0, 0)
        time.sleep(240)
    except libvirt.libvirtError as e:
        print('Warning')
        print(e)

def vm_snapshot(libvirt_conn, vm_name, vm_info, vm_devs, devs_to_snapshot, backupset, args):
    try:
        vm = libvirt_conn.lookupByName(vm_name)
    except libvirt.libvirtError as e:
        # Error code 42 = Domain not found
        if (e.get_error_code() == 42):
            print(e)
            exit(1)
        else:
            raise(e)

    xml = "<domainsnapshot><name>b%03d.snapshot</name><disks>" % (backupset)
    for dev in devs_to_snapshot:
        xml += "<disk name='%s'><source file='%s.b%03d.i%05d.img'/></disk>"  % (vm_info[dev]['chain'][0], vm_info[dev]['chain'][len(vm_info[dev]['chain'])-1][:-4], backupset, vm_info[dev]['nr']+1)
    for dev in vm_devs:
        if not dev in devs_to_snapshot:
            xml += "<disk name='%s' snapshot='no' />" % (dev)
    xml += "</disks></domainsnapshot>"

    snapshot = vm.snapshotCreateXML(xml, libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY + libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE + libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC)
    snapshot.delete(libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY)
    for dev in devs_to_snapshot:
        if len(vm_info[dev]['chain']) < 2:
            img_copy_to_backup_dir(vm_info[dev]['chain'][0], "%s.b%03d.%s.base.img" % (vm_name, backupset, dev), args)
        else:
            new_name = "%s.b%03d.%s.i%05d.%s.%d.img" % (vm_name, backupset, dev, vm_info[dev]['nr'], args.intervals[0][0], 0)
            img_copy_to_backup_dir(vm_info[dev]['chain'][0], new_name, args)
            if vm_info[dev]['nr']-1 == 0:
                baseimage = "%s.b%03d.%s.base.img" % (vm_name, backupset, dev)
            else:
                baseimage = "%s.b%03d.%s.i%05d.%s.%d.img" % (vm_name, backupset, dev, vm_info[dev]['nr']-1, args.intervals[0][0], 1)
            img_rebase(args.backup_dir + '/' + new_name, args.backup_dir, baseimage)

def vm_backup(libvirt_conn, vm, args):
    blockdevs = vm_get_blockdevs(libvirt_conn, vm[0])

    if (len(vm) == 1):
        vm.append([])
        for dev in blockdevs:
            vm[1].append(dev)

    vm_info = {}

    active_backupset = 0
    incomplete_snapshots = []
    for dev in vm[1]:
        if not dev in blockdevs:
            raise LookupError('Unknown block device for domain ' + vm[0] + ': ' + dev)

        snapshot_chain = get_snapshot_chain(blockdevs[dev])
        if len(snapshot_chain) == 2:
            imginfo = snapshot_chain[0].split('.')
            backupset = imginfo[-3]
            nr = imginfo[-2]
            if not re.compile('^b\d+$').match(backupset):
                raise ValueError('Cannot get backupset from snapshot filename')
            if not re.compile('^i\d+$').match(nr):
                raise ValueError('Cannot get incremental backup number from snapshot filename')
            backupset = int(backupset[1:])
            nr = int(nr[1:])
        elif len(snapshot_chain) > 2:
            raise ValueError('Snapshot chain too long')
        else:
            nr = 0
            backupset = 1
            incomplete_snapshots.append(dev)
        if args.new_chain:
            nr = 0

        if backupset > active_backupset:
            active_backupset = backupset

        vm_info[dev] = { 'backupset': backupset, 'nr': nr, 'chain': snapshot_chain }

    if active_backupset == 0 or args.new_chain:
        active_backupset += 1
        args.new_chain = True
        incomplete_snapshots = vm[1]

    check_backup_chain(vm[0], "b%03d" % (active_backupset), vm[1], args)

    # backup base image
    if len(incomplete_snapshots) > 0 or args.new_chain:
        vm_commit_all(libvirt_conn, vm[0], vm_info, incomplete_snapshots, args)
        vm_trim(libvirt_conn, vm[0])
        vm_snapshot(libvirt_conn, vm[0], vm_info, blockdevs, incomplete_snapshots, active_backupset, args)
    else:
        # move existing backups to make room for new incremental
        interval = args.interval
        for dev in vm[1]:
            # check if there is an image that can move to interval.0
            if interval > 0:
                interval_name = args.intervals[interval-1][0]
                backupset = "b%03d" % (active_backupset)
                if not backupset in archive_info[vm[0]]:
                    raise Exception('No backup images found in backupset')
                if not dev in archive_info[vm[0]][backupset]:
                    raise Exception('No backup images for drive found in backupset')
                if not interval_name in archive_info[vm[0]][backupset][dev]['images']:
                    continue # no backup yet
                imagecount = len(archive_info[vm[0]][backupset][dev]['images'][interval_name])
                if imagecount == 1:
                    continue # no backup yet

                lowest_image = max(archive_info[vm[0]][backupset][dev]['images'][interval_name].keys())
                old_filename = archive_info[vm[0]][backupset][dev]['images'][interval_name][lowest_image]
                new_interval_name = args.intervals[interval][0]
                new_filename = old_filename.replace(interval_name + ".%d" % (lowest_image), "%s.0" % (new_interval_name))
                new_path = Path(args.backup_dir + '/' + new_filename)
                if not new_interval_name in archive_info[vm[0]][backupset][dev]['images']:
                    archive_info[vm[0]][backupset][dev]['images'][new_interval_name] = {}
                if 0 in archive_info[vm[0]][backupset][dev]['images'][new_interval_name]:
                    img_rotate_interval(vm[0], active_backupset, interval, dev, vm_info, args)
                if new_path.exists():
                    raise Exception(new_filename + ' already exists')
                img_rename(args.backup_dir + '/' + old_filename, args.backup_dir + '/' + new_filename)
                del archive_info[vm[0]][backupset][dev]['images'][interval_name][lowest_image]
                archive_info[vm[0]][backupset][dev]['images'][new_interval_name][0] = new_filename
                img_rebase(args.backup_dir + '/' + archive_info[vm[0]][backupset][dev]['images'][interval_name][lowest_image-1], args.backup_dir, new_filename)
                if 1 in archive_info[vm[0]][backupset][dev]['images'][new_interval_name]:
                    img_rebase(args.backup_dir + '/' + new_filename, args.backup_dir, archive_info[vm[0]][backupset][dev]['images'][new_interval_name][1])
            else:
                img_rotate_interval(vm[0], active_backupset, interval, dev, vm_info, args)

        if interval == 0:
            # create incremental snapshot
            vm_trim(libvirt_conn, vm[0])
            vm_snapshot(libvirt_conn, vm[0], vm_info, blockdevs, vm[1], active_backupset, args)
            vm_commit_first(libvirt_conn, vm[0], vm_info, vm[1], args)

def init_archive_info(args):
    backup_path = Path(args.backup_dir)
    if not backup_path.exists():
        raise NotADirectoryError('Backup path not found')

    for image in backup_path.glob('*.img'):
        if not image.is_file():
            continue

        imgdata = image.name.split('.')
        # 0: domain name, 1: b<nr>, 2: <drive>, 3: i<nr>[-<nr>] | base, 4: <interval>, 5: <nr>, 6: img
        domain = imgdata[0]
        backupset = imgdata[1]
        drive = imgdata[2]
        if len(imgdata) == 5:
            interval = imgdata[3]
        else:
            interval = imgdata[4]
            nr = int(imgdata[5])

        if not domain in archive_info:
            archive_info[domain] = {}
        if not backupset in archive_info[domain]:
            archive_info[domain][backupset] = {}
        if not drive in archive_info[domain][backupset]:
            archive_info[domain][backupset][drive] = {'intervals':[], 'images':{}, 'image_count':0, 'chain': []}
        if interval != 'base':
            if not interval in archive_info[domain][backupset][drive]['intervals']:
                archive_info[domain][backupset][drive]['intervals'].append(interval)
            if not interval in archive_info[domain][backupset][drive]['images']:
                archive_info[domain][backupset][drive]['images'][interval] = {}
            archive_info[domain][backupset][drive]['images'][interval][nr] = image.name
        if not interval in archive_info[domain][backupset][drive]['images']:
            archive_info[domain][backupset][drive]['images'][interval] = []
        archive_info[domain][backupset][drive]['image_count'] += 1

        snapshot_chain = get_snapshot_chain(args.backup_dir + '/' + image.name)
        if len(snapshot_chain) > len(archive_info[domain][backupset][drive]['chain']):
            archive_info[domain][backupset][drive]['chain'] = snapshot_chain

        backing_file = get_backing_file(args.backup_dir + '/' + image.name)
        if interval == 'base' and backing_file != '':
            raise ValueError('The base image ' + image.name + ' must not have a backing file')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backup virtual machines.')
    parser.add_argument('domains', metavar='DOM[:drive0,drive1,...]', nargs='+', help='domains to backup (optional: limit to drives)')
    parser.add_argument('--backup-dir', dest='backup_dir', action='store', default='/var/vmbackup', help='Backup directory (default: /var/vmbackup)')
    parser.add_argument('--intervals', dest='intervals', action='store', default='daily:7,weekly:4,monthly,yearly:10', help='Comma separated list of backup intervals and number of backups to keep (default: daily:7,weekly:4,monthly:12,yearly:10)')
    parser.add_argument('--interval', dest='interval', action='store', default='', help='Backup interval (default: lowest)')
    parser.add_argument('--new-chain', dest='new_chain', action='store_true', default=False, help='create new backup chain (default: no)')
    parser.add_argument('--copy', dest='copy', action='store_true', default=False, help='copy file instead of using qemu-img convert, ignored if compression is enabled (default: no)')
    parser.add_argument('--compress', dest='compress', action='store_true', default=False, help='use qemu-img convert to compress image files (default: no)')
    parser.add_argument('--omit-unsafe', dest='omit_unsafe', action='store_true', default=False, help='do not use -U on qemu-img info (default: no)')
    args = parser.parse_args()

    if not lock_acquire('/tmp/qemu-backup.lock'):
        print("another instance is running")
        exit(1)

    args.intervals = args.intervals.split(',')
    intervals = []
    for interval in args.intervals:
        interval = interval.split(':')
        if (len(interval) == 1):
            interval.append(3)
        else:
            try:
                interval[1] = int(interval[1])
            except:
                raise TypeError('Must be an integer: ' + interval[1])
        if (interval[1] <= 0):
            raise ValueError('Number of backups to keep must be positive.')
        intervals.append(interval)
    args.intervals = intervals

    if args.interval == '':
        args.interval = args.intervals[0][0]

    i = 0
    for interval in args.intervals:
        if args.interval == interval[0]:
            args.interval = i
            break
        i += 1

    domains = []
    for domain in args.domains:
        domain = domain.split(':')
        if (len(domain) > 2):
            raise ValueError('Invalid drives for domain ' + domain[0])
        if (len(domain) == 2):
            domain[1] = domain[1].split(',')
        domains.append(domain)
    args.domains = domains
    omit_unsafe = args.omit_unsafe

    init_archive_info(args)

    #connect to hypervisor running on localhost
    conn = libvirt.open('qemu:///system')

    for vm in args.domains:
        vm_backup(conn, vm, args)

    conn.close()

exit(0)
