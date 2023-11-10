#!/usr/bin/env bash

# Copyright (C) 2019 Red Hat, Inc. All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions
# of the GNU General Public License v.2.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA


SKIP_WITH_LVMPOLLD=1

. lib/inittest

aux have_raid 1 3 0 || skip
PROGRESS=0
aux have_raid 1 15 0 && PROGRESS=1

# Use smallest regionsize to save VG space
regionsize=$(getconf PAGESIZE) # in bytes
let pageregions=regionsize*8  # number of regions per MD bitmap page

# in KiB
let regionsize=regionsize/1024

# in MiB
let lvsz=pageregions*regionsize/1024
let lvext=lvsz/8

aux prepare_pvs 2 $(($lvsz + 3 * $lvext))
get_devs
vgcreate -s 4k $vg ${DEVICES[@]}

# Keep $dev1 & $dev2 always open via small active LVs.
# This trick avoids race on system with scanning udev service
# when device is 'in-use' and we cleared _rimage & _rmeta.
lvcreate -l1 $vg "$dev1"
lvcreate -l1 $vg "$dev2"

sector=$(( $(get first_extent_sector "$dev2") + 2048 ))
aux zero_dev "$dev1" "${sector}:"
aux delayzero_dev "$dev2"  0 10 "${sector}:"

# Create raid1 LV consuming 1 MD bitmap page
lvcreate --yes --type raid1 --regionsize ${regionsize}K -L$(($lvsz-$lvext))M -n $lv1 $vg

lvs -a $vg

not check lv_field $vg/$lv1 sync_percent "100.00"
check lv_field $vg/$lv1 size "$(($lvsz-$lvext)).00m" $vg/$lv1
aux wait_for_sync $vg $lv1
check lv_field $vg/$lv1 sync_percent "100.00"
check lv_field $vg/$lv1 region_size "4.00k"

# to slow down extension - slowdown readings
aux delayzero_dev "$dev1"  50 0 "${sector}:"
aux delayzero_dev "$dev2"  0 50 "${sector}:"

# Extend so that full MD bitmap page is consumed
lvextend -y -L+${lvext}M $vg/$lv1
if [ $PROGRESS -eq 1 ]
then
# Even with delayed devices wre are catching races here.
should not check lv_field $vg/$lv1 sync_percent "100.00"
check lv_field $vg/$lv1 size "$(($lvsz)).00m" $vg/$lv1
fi
aux wait_for_sync $vg $lv1
check lv_field $vg/$lv1 sync_percent "100.00"

# Extend so that another MD bitmap page is allocated
lvextend -y -L+${lvext}M $vg/$lv1
if [ $PROGRESS -eq 1 ]
then
	# Even with delayed devices wre are catching races here.
	should not check lv_field $vg/$lv1 sync_percent "100.00"
else
	check lv_field $vg/$lv1 sync_percent "100.00"
fi

aux enable_dev "$dev1"
aux enable_dev "$dev2"

aux wait_for_sync $vg $lv1
check lv_field $vg/$lv1 sync_percent "100.00"
check lv_field $vg/$lv1 size "$(($lvsz+$lvext)).00m" $vg/$lv1

vgremove -ff $vg
