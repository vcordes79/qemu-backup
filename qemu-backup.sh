#!/bin/sh

DOM=$(date +%-d)
DOW=$(date +%-u)
MON=$(date +%-m)

if [ $DOM -eq 1 -a $MON -eq 1 ]; then
  qemu-backup.py --interval yearly $*
fi

if [ $DOM -eq 1 ]; then
  qemu-backup.py --interval monthly $*
fi

if [ $DOW -eq 0 ]; then
  qemu-backup.py --interval weekly $*
fi

qemu-backup.py $*
