#!/bin/sh
# Copyright (C) 2012,2016 Red Hat, Inc. All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions
# of the GNU General Public License v.2.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

# 'Exercise some lvcreate diagnostics'

SKIP_WITH_LVMLOCKD=1
SKIP_WITH_LVMPOLLD=1

. lib/inittest

# FIXME  update test to make something useful on <16T
aux can_use_16T || skip

aux have_raid 1 3 0 || skip

segtypes="raid5"
aux have_raid4 && segtypes="raid4 raid5"

# Prepare 5x ~1P sized devices
aux prepare_pvs 5 1000000000

vgcreate $vg1 $(< DEVICES)

aux lvmconf 'devices/issue_discards = 1'

# Delay PVs so that resynchronization doesn't fill too much space
for device in $(< DEVICES)
do
	aux delay_dev "$device" 0 10  $(get first_extent_sector "$device")
done

# bz837927 START

#
# Create large RAID LVs
#

# 200 TiB raid1
lvcreate --type raid1 -m 1 -L 200T -n $lv1 $vg1 --nosync
check lv_field $vg1/$lv1 size "200.00t"
check raid_leg_status $vg1 $lv1 "AA"
lvremove -ff $vg1

# 1 PiB raid1
lvcreate --type raid1 -m 1 -L 1P -n $lv1 $vg1 --nosync
check lv_field $vg1/$lv1 size "1.00p"
check raid_leg_status $vg1 $lv1 "AA"
lvremove -ff $vg1

# 750 TiB raid4/5
for segtype in $segtypes; do
        lvcreate --type $segtype -i 3 -L 750T -n $lv1 $vg1 --nosync
        check lv_field $vg1/$lv1 size "750.00t"
        check raid_leg_status $vg1 $lv1 "AAAA"
        lvremove -ff $vg1
done

#
# Extending large 200 TiB RAID LV to 400 TiB (belong in different script?)
#
lvcreate --type raid1 -m 1 -L 200T -n $lv1 $vg1 --nosync
check lv_field $vg1/$lv1 size "200.00t"
check raid_leg_status $vg1 $lv1 "AA"
lvextend -L +200T $vg1/$lv1
check lv_field $vg1/$lv1 size "400.00t"
check raid_leg_status $vg1 $lv1 "AA"
lvremove -ff $vg1


# Check --nosync is rejected for raid6
if aux have_raid 1 9 0 ; then
	not lvcreate --type raid6 -i 3 -L 750T -n $lv1 $vg1 --nosync
fi

# 750 TiB raid6
lvcreate --type raid6 -i 3 -L 750T -n $lv1 $vg1
check lv_field $vg1/$lv1 size "750.00t"
check raid_leg_status $vg1 $lv1 "aaaaa"
lvremove -ff $vg1

# 1 PiB raid6, then extend up to 2 PiB
lvcreate --type raid6 -i 3 -L 1P -n $lv1 $vg1
check lv_field $vg1/$lv1 size "1.00p"
check raid_leg_status $vg1 $lv1 "aaaaa"
lvextend -L +1P $vg1/$lv1
check lv_field $vg1/$lv1 size "2.00p"
check raid_leg_status $vg1 $lv1 "aaaaa"
lvremove -ff $vg1

#
# Convert large 200 TiB linear to RAID1 (belong in different test script?)
#
lvcreate -aey -L 200T -n $lv1 $vg1
lvconvert --type raid1 -m 1 $vg1/$lv1
check lv_field $vg1/$lv1 size "200.00t"
check raid_leg_status $vg1 $lv1 "aa"
lvremove -ff $vg1

# bz837927 END

vgremove -ff $vg1
