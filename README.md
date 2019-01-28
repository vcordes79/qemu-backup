# qemu-backup
rsnapshot like backup for qemu

I wrote a little script to take rsnapshot like backups with configurable
intervals (default ist daily, weekly, monthly, yearly). So basically I
take incremental snapshots at the lowest interval, copy them to the
backup location and consolidate the backups based on the retention rules.

To work the image files have to be qcow2 with an file extension of img.
What happens internally is, that I use a base image and a snapshot for
the changes:

vm.base.img <-- vm.snapshot.img

When a new backup is created, a new snapshot is made:

vm.base.img <-- vm.snapshot.img <-- vm.snapshot.new.img

The vm.snapshot.img is then copied and after that merged into the base.
The backing files in the backup dir are adjusted via qemu-img rebase.
The backup dir should contain files like this:

vm.b001.sda.i00001.daily.0.img

b001 is the number of the backup set i00001 the number of the
incremental backup. So a complete chain could look like:

vm.b001.sda.base.img
<- vm.b001.sda.i00001.monthly.0.img
<- vm.b001.sda.i00002-i00004.weekly.3.img
<- vm.b001.sda.i00005.weekly.2.img
<- vm.b001.sda.i00006.weekly.1.img
<- vm.b001.sda.i00007.weekly.0.img
<- vm.b001.sda.i00008-i00009.daily.6.img
<- vm.b001.sda.i00010.daily.5.img
<- vm.b001.sda.i00011.daily.4.img
<- vm.b001.sda.i00012.daily.3.img
<- vm.b001.sda.i00013.daily.2.img
<- vm.b001.sda.i00014.daily.1.img
<- vm.b001.sda.i00015.daily.0.img

Comments and feedback welcome.
