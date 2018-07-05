#!/usr/bin/env python3

# Copyright (C) 2015-2016 Red Hat, Inc. All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions
# of the GNU General Public License v.2.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

# noinspection PyUnresolvedReferences
import dbus
# noinspection PyUnresolvedReferences
from dbus.mainloop.glib import DBusGMainLoop
import unittest
import time
import pyudev
from testlib import *
import testlib

g_tmo = 0

# Prefix on created objects to enable easier clean-up
g_prefix = os.getenv('PREFIX', '')

# Use the session bus instead of the system bus
use_session = os.getenv('LVM_DBUSD_USE_SESSION', False)

# Only use the devices listed in the ENV variable
pv_device_list = os.getenv('LVM_DBUSD_PV_DEVICE_LIST', None)

# Default is to test all modes
# 0 == Only test fork & exec mode
# 1 == Test both fork & exec & lvm shell mode (default)
# Other == Test just lvm shell mode
test_shell = os.getenv('LVM_DBUSD_TEST_MODE', 1)

# Empty options dictionary (EOD)
EOD = dbus.Dictionary({}, signature=dbus.Signature('sv'))
# Base interfaces on LV objects
LV_BASE_INT = (LV_COMMON_INT, LV_INT)

if use_session:
	bus = dbus.SessionBus(mainloop=DBusGMainLoop())
else:
	bus = dbus.SystemBus(mainloop=DBusGMainLoop())

# If we have multiple clients we will globally disable introspection
# validation to limit the massive amount of introspection calls we make as
# that method prevents things from executing concurrently
if pv_device_list:
	testlib.validate_introspection = False


def vg_n():
	return g_prefix + rs(8, '_vg')


def lv_n(suffix=None):
	if not suffix:
		s = '_lv'
	else:
		s = suffix
	return g_prefix + rs(8, s)


def get_objects():
	rc = {
		MANAGER_INT: [], PV_INT: [], VG_INT: [], LV_INT: [],
		THINPOOL_INT: [], JOB_INT: [], SNAPSHOT_INT: [], LV_COMMON_INT: [],
		CACHE_POOL_INT: [], CACHE_LV_INT: []}

	object_manager_object = bus.get_object(
		BUS_NAME, "/com/redhat/lvmdbus1", introspect=False)

	manager_interface = dbus.Interface(object_manager_object,
							"org.freedesktop.DBus.ObjectManager")

	objects = manager_interface.GetManagedObjects()

	for object_path, v in objects.items():
		proxy = ClientProxy(bus, object_path, v)
		for interface, prop in v.items():
			if interface == PV_INT:
				# If we have a list of PVs to use, lets only use those in
				# the list
				# noinspection PyUnresolvedReferences
				if pv_device_list and not (proxy.Pv.Name in pv_device_list):
					continue
			rc[interface].append(proxy)

	return rc, bus


def set_execution(lvmshell, test_result):
	if lvmshell:
		m = 'lvm shell (non-fork)'
	else:
		m = "forking & exec'ing"

	lvm_manager = dbus.Interface(bus.get_object(
		BUS_NAME, "/com/redhat/lvmdbus1/Manager", introspect=False),
		"com.redhat.lvmdbus1.Manager")
	rc = lvm_manager.UseLvmShell(lvmshell)

	if rc:
		std_err_print('Successfully changed execution mode to "%s"' % m)
	else:
		std_err_print('ERROR: Failed to change execution mode to "%s"' % m)
		test_result.register_fail()
	return rc


# noinspection PyUnresolvedReferences
class TestDbusService(unittest.TestCase):
	def setUp(self):
		# Because of the sensitive nature of running LVM tests we will only
		# run if we have PVs and nothing else, so that we can be confident that
		# we are not mucking with someones data on their system
		self.objs, self.bus = get_objects()
		if len(self.objs[PV_INT]) == 0:
			std_err_print('No PVs present exiting!')
			sys.exit(1)
		if len(self.objs[MANAGER_INT]) != 1:
			std_err_print('Expecting a manager object!')
			sys.exit(1)

		# We will skip the vg device number check if the test user
		# has specified a PV list
		if pv_device_list is None:
			if len(self.objs[VG_INT]) != 0:
				std_err_print('Expecting no VGs to exist!')
				sys.exit(1)

		self.pvs = []
		for p in self.objs[PV_INT]:
			self.pvs.append(p.Pv.Name)

	def tearDown(self):
		# If we get here it means we passed setUp, so lets remove anything
		# and everything that remains, besides the PVs themselves
		self.objs, self.bus = get_objects()

		if pv_device_list is None:
			for v in self.objs[VG_INT]:
				self.handle_return(
					v.Vg.Remove(
						dbus.Int32(g_tmo),
						EOD))
		else:
			for p in self.objs[PV_INT]:
				# When we remove a VG for a PV it could ripple across multiple
				# VGs, so update each PV while removing each VG, to ensure
				# the properties are current and correct.
				p.update()
				if p.Pv.Vg != '/':
					v = ClientProxy(self.bus, p.Pv.Vg, interfaces=(VG_INT, ))
					self.handle_return(
						v.Vg.Remove(dbus.Int32(g_tmo), EOD))

		# Check to make sure the PVs we had to start exist, else re-create
		# them
		if len(self.pvs) != len(self.objs[PV_INT]):
			for p in self.pvs:
				found = False
				for pc in self.objs[PV_INT]:
					if pc.Pv.Name == p:
						found = True
						break

				if not found:
					# print('Re-creating PV=', p)
					self._pv_create(p)

	def _check_consistency(self):
		# Only do consistency checks if we aren't running the unit tests
		# concurrently
		if pv_device_list is None:
			self.assertEqual(self._refresh(), 0)

	def handle_return(self, rc):
		if isinstance(rc, (tuple, list)):
			# We have a tuple returned
			if rc[0] != '/':
				return rc[0]
			else:
				return self._wait_for_job(rc[1])
		else:
			if rc == '/':
				return rc
			else:
				return self._wait_for_job(rc)

	def _pv_create(self, device):

		pv_path = self.handle_return(
			self.objs[MANAGER_INT][0].Manager.PvCreate(
				dbus.String(device), dbus.Int32(g_tmo), EOD)
		)
		self.assertTrue(pv_path is not None and len(pv_path) > 0)
		return pv_path

	def _manager(self):
		return self.objs[MANAGER_INT][0]

	def _refresh(self):
		return self._manager().Manager.Refresh()

	def test_refresh(self):
		self._check_consistency()

	def test_version(self):
		rc = self.objs[MANAGER_INT][0].Manager.Version
		self.assertTrue(rc is not None and len(rc) > 0)
		self._check_consistency()

	def _vg_create(self, pv_paths=None):

		if not pv_paths:
			pv_paths = [self.objs[PV_INT][0].object_path]

		vg_name = vg_n()

		vg_path = self.handle_return(
			self.objs[MANAGER_INT][0].Manager.VgCreate(
				dbus.String(vg_name),
				dbus.Array(pv_paths, signature=dbus.Signature('o')),
				dbus.Int32(g_tmo),
				EOD))

		self.assertTrue(vg_path is not None and len(vg_path) > 0)
		return ClientProxy(self.bus, vg_path, interfaces=(VG_INT, ))

	def test_vg_create(self):
		self._vg_create()
		self._check_consistency()

	def test_vg_delete(self):
		vg = self._vg_create().Vg

		self.handle_return(
			vg.Remove(dbus.Int32(g_tmo), EOD))
		self._check_consistency()

	def _pv_remove(self, pv):
		rc = self.handle_return(
			pv.Pv.Remove(dbus.Int32(g_tmo), EOD))
		return rc

	def test_pv_remove_add(self):
		target = self.objs[PV_INT][0]

		# Remove the PV
		rc = self._pv_remove(target)
		self.assertTrue(rc == '/')
		self._check_consistency()

		# Add it back
		rc = self._pv_create(target.Pv.Name)[0]
		self.assertTrue(rc == '/')
		self._check_consistency()

	def _create_raid5_thin_pool(self, vg=None):

		if not vg:
			pv_paths = []
			for pp in self.objs[PV_INT]:
				pv_paths.append(pp.object_path)

			vg = self._vg_create(pv_paths).Vg

		lv_meta_path = self.handle_return(
			vg.LvCreateRaid(
				dbus.String("meta_r5"),
				dbus.String("raid5"),
				dbus.UInt64(mib(4)),
				dbus.UInt32(0),
				dbus.UInt32(0),
				dbus.Int32(g_tmo),
				EOD)
		)

		lv_data_path = self.handle_return(
			vg.LvCreateRaid(
				dbus.String("data_r5"),
				dbus.String("raid5"),
				dbus.UInt64(mib(16)),
				dbus.UInt32(0),
				dbus.UInt32(0),
				dbus.Int32(g_tmo),
				EOD)
		)

		thin_pool_path = self.handle_return(
			vg.CreateThinPool(
				dbus.ObjectPath(lv_meta_path),
				dbus.ObjectPath(lv_data_path),
				dbus.Int32(g_tmo), EOD)
		)

		# Get thin pool client proxy
		thin_pool = ClientProxy(self.bus, thin_pool_path,
								interfaces=(LV_COMMON_INT,
											LV_INT,
											THINPOOL_INT))

		return vg, thin_pool

	def test_meta_lv_data_lv_props(self):
		# Ensure that metadata lv and data lv for thin pools and cache pools
		# point to a valid LV
		(vg, thin_pool) = self._create_raid5_thin_pool()

		# Check properties on thin pool
		self.assertTrue(thin_pool.ThinPool.DataLv != '/')
		self.assertTrue(thin_pool.ThinPool.MetaDataLv != '/')

		(vg, cache_pool) = self._create_cache_pool(vg)

		self.assertTrue(cache_pool.CachePool.DataLv != '/')
		self.assertTrue(cache_pool.CachePool.MetaDataLv != '/')

		# Cache the thin pool
		cached_thin_pool_path = self.handle_return(
			cache_pool.CachePool.CacheLv(
				dbus.ObjectPath(thin_pool.object_path),
				dbus.Int32(g_tmo), EOD)
		)

		# Get object proxy for cached thin pool
		cached_thin_pool_object = ClientProxy(self.bus, cached_thin_pool_path,
												interfaces=(LV_COMMON_INT,
															LV_INT,
															THINPOOL_INT))

		# Check properties on cache pool
		self.assertTrue(cached_thin_pool_object.ThinPool.DataLv != '/')
		self.assertTrue(cached_thin_pool_object.ThinPool.MetaDataLv != '/')

	def _lookup(self, lvm_id):
		return self.objs[MANAGER_INT][0].Manager.LookUpByLvmId(lvm_id)

	def test_lookup_by_lvm_id(self):
		# For the moment lets just lookup what we know about which is PVs
		# When we start testing VGs and LVs we will test lookups for those
		# during those unit tests
		for p in self.objs[PV_INT]:
			rc = self._lookup(p.Pv.Name)
			self.assertTrue(rc is not None and rc != '/')

		# Search for something which doesn't exist
		rc = self._lookup('/dev/null')
		self.assertTrue(rc == '/')

	def test_vg_extend(self):
		# Create a VG
		self.assertTrue(len(self.objs[PV_INT]) >= 2)

		if len(self.objs[PV_INT]) >= 2:
			pv_initial = self.objs[PV_INT][0]
			pv_next = self.objs[PV_INT][1]

			vg = self._vg_create([pv_initial.object_path]).Vg

			path = self.handle_return(
				vg.Extend(
					dbus.Array([pv_next.object_path], signature="o"),
					dbus.Int32(g_tmo), EOD)
			)
			self.assertTrue(path == '/')
			self._check_consistency()

	# noinspection PyUnresolvedReferences
	def test_vg_reduce(self):
		self.assertTrue(len(self.objs[PV_INT]) >= 2)

		if len(self.objs[PV_INT]) >= 2:
			vg = self._vg_create(
				[self.objs[PV_INT][0].object_path,
					self.objs[PV_INT][1].object_path]).Vg

			path = self.handle_return(
				vg.Reduce(
					dbus.Boolean(False), dbus.Array([vg.Pvs[0]], signature='o'),
					dbus.Int32(g_tmo), EOD)
			)
			self.assertTrue(path == '/')
			self._check_consistency()

	# noinspection PyUnresolvedReferences
	def test_vg_rename(self):
		vg = self._vg_create().Vg

		mgr = self.objs[MANAGER_INT][0].Manager

		# Do a vg lookup
		path = mgr.LookUpByLvmId(dbus.String(vg.Name))

		vg_name_start = vg.Name

		prev_path = path
		self.assertTrue(path != '/', "%s" % (path))

		# Create some LVs in the VG
		for i in range(0, 5):
			lv_t = self._create_lv(size=mib(4), vg=vg)
			full_name = "%s/%s" % (vg_name_start, lv_t.LvCommon.Name)
			lv_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(lv_path == lv_t.object_path)

		new_name = 'renamed_' + vg.Name

		path = self.handle_return(
			vg.Rename(dbus.String(new_name), dbus.Int32(g_tmo), EOD))
		self.assertTrue(path == '/')
		self._check_consistency()

		# Do a vg lookup
		path = mgr.LookUpByLvmId(dbus.String(new_name))
		self.assertTrue(path != '/', "%s" % (path))
		self.assertTrue(prev_path == path, "%s != %s" % (prev_path, path))

		# Go through each LV and make sure it has the correct path back to the
		# VG
		vg.update()

		lv_paths = vg.Lvs
		self.assertTrue(len(lv_paths) == 5)

		for l in lv_paths:
			lv_proxy = ClientProxy(self.bus, l,
									interfaces=(LV_COMMON_INT,)).LvCommon
			self.assertTrue(
				lv_proxy.Vg == vg.object_path, "%s != %s" %
				(lv_proxy.Vg, vg.object_path))
			full_name = "%s/%s" % (new_name, lv_proxy.Name)
			lv_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(
				lv_path == lv_proxy.object_path, "%s != %s" %
				(lv_path, lv_proxy.object_path))

	def _verify_hidden_lookups(self, lv_common_object, vgname):
		mgr = self.objs[MANAGER_INT][0].Manager

		hidden_lv_paths = lv_common_object.HiddenLvs

		for h in hidden_lv_paths:
			h_lv = ClientProxy(self.bus, h,
								interfaces=(LV_COMMON_INT,)).LvCommon

			if len(h_lv.HiddenLvs) > 0:
				self._verify_hidden_lookups(h_lv, vgname)

			full_name = "%s/%s" % (vgname, h_lv.Name)
			# print("Hidden check %s" % (full_name))
			lookup_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(lookup_path != '/')
			self.assertTrue(lookup_path == h_lv.object_path)

			# Lets's strip off the '[ ]' and make sure we can find
			full_name = "%s/%s" % (vgname, h_lv.Name[1:-1])
			# print("Hidden check %s" % (full_name))

			lookup_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(lookup_path != '/')
			self.assertTrue(lookup_path == h_lv.object_path)

	def test_vg_rename_with_thin_pool(self):

		(vg, thin_pool) = self._create_raid5_thin_pool()

		vg_name_start = vg.Name
		mgr = self.objs[MANAGER_INT][0].Manager

		# noinspection PyTypeChecker
		self._verify_hidden_lookups(thin_pool.LvCommon, vg_name_start)

		for i in range(0, 5):
			lv_name = lv_n()

			thin_lv_path = self.handle_return(
				thin_pool.ThinPool.LvCreate(
					dbus.String(lv_name),
					dbus.UInt64(mib(16)),
					dbus.Int32(g_tmo),
					EOD))

			self.assertTrue(thin_lv_path != '/')

			full_name = "%s/%s" % (vg_name_start, lv_name)

			lookup_lv_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(
				thin_lv_path == lookup_lv_path,
				"%s != %s" % (thin_lv_path, lookup_lv_path))

		# Rename the VG
		new_name = 'renamed_' + vg.Name
		path = self.handle_return(
			vg.Rename(dbus.String(new_name), dbus.Int32(g_tmo), EOD))

		self.assertTrue(path == '/')
		self._check_consistency()

		# Go through each LV and make sure it has the correct path back to the
		# VG
		vg.update()
		thin_pool.update()

		lv_paths = vg.Lvs

		for l in lv_paths:
			lv_proxy = ClientProxy(self.bus, l,
									interfaces=(LV_COMMON_INT,)).LvCommon
			self.assertTrue(
				lv_proxy.Vg == vg.object_path, "%s != %s" %
				(lv_proxy.Vg, vg.object_path))
			full_name = "%s/%s" % (new_name, lv_proxy.Name)
			# print('Full Name %s' % (full_name))
			lv_path = mgr.LookUpByLvmId(dbus.String(full_name))
			self.assertTrue(
				lv_path == lv_proxy.object_path, "%s != %s" %
				(lv_path, lv_proxy.object_path))

		# noinspection PyTypeChecker
		self._verify_hidden_lookups(thin_pool.LvCommon, new_name)

	def _test_lv_create(self, method, params, vg, proxy_interfaces=None):
		lv = None

		path = self.handle_return(method(*params))
		self.assertTrue(vg)

		if path:
			lv = ClientProxy(self.bus, path, interfaces=proxy_interfaces)

		# We are quick enough now that we can get VolumeType changes from
		# 'I' to 'i' between the time it takes to create a RAID and it returns
		# and when we refresh state here.  Not sure how we can handle this as
		# we cannot just sit and poll all the time for changes...
		# self._check_consistency()
		return lv

	def test_lv_create(self):
		vg = self._vg_create().Vg
		self._test_lv_create(
			vg.LvCreate,
			(dbus.String(lv_n()), dbus.UInt64(mib(4)),
			dbus.Array([], signature='(ott)'), dbus.Int32(g_tmo),
			EOD), vg, LV_BASE_INT)

	def test_lv_create_job(self):

		vg = self._vg_create().Vg
		(object_path, job_path) = vg.LvCreate(
			dbus.String(lv_n()), dbus.UInt64(mib(4)),
			dbus.Array([], signature='(ott)'), dbus.Int32(0),
			EOD)

		self.assertTrue(object_path == '/')
		self.assertTrue(job_path != '/')
		object_path = self._wait_for_job(job_path)
		self.assertTrue(object_path != '/')

	def test_lv_create_linear(self):

		vg = self._vg_create().Vg
		self._test_lv_create(
			vg.LvCreateLinear,
			(dbus.String(lv_n()), dbus.UInt64(mib(4)), dbus.Boolean(False),
			dbus.Int32(g_tmo), EOD),
			vg, LV_BASE_INT)

	def test_lv_create_striped(self):
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg
		self._test_lv_create(
			vg.LvCreateStriped,
			(dbus.String(lv_n()), dbus.UInt64(mib(4)),
			dbus.UInt32(2), dbus.UInt32(8), dbus.Boolean(False),
			dbus.Int32(g_tmo), EOD),
			vg, LV_BASE_INT)

	def test_lv_create_mirror(self):
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg
		self._test_lv_create(
			vg.LvCreateMirror,
			(dbus.String(lv_n()), dbus.UInt64(mib(4)), dbus.UInt32(2),
			dbus.Int32(g_tmo), EOD), vg, LV_BASE_INT)

	def test_lv_create_raid(self):
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg
		self._test_lv_create(
			vg.LvCreateRaid,
			(dbus.String(lv_n()), dbus.String('raid5'), dbus.UInt64(mib(16)),
			dbus.UInt32(2), dbus.UInt32(8), dbus.Int32(g_tmo),
			EOD),
			vg,
			LV_BASE_INT)

	def _create_lv(self, thinpool=False, size=None, vg=None):

		interfaces = list(LV_BASE_INT)

		if thinpool:
			interfaces.append(THINPOOL_INT)

		if not vg:
			pv_paths = []
			for pp in self.objs[PV_INT]:
				pv_paths.append(pp.object_path)

			vg = self._vg_create(pv_paths).Vg

		if size is None:
			size = mib(4)

		return self._test_lv_create(
			vg.LvCreateLinear,
			(dbus.String(lv_n()), dbus.UInt64(size),
			dbus.Boolean(thinpool), dbus.Int32(g_tmo), EOD),
			vg, interfaces)

	def test_lv_create_rounding(self):
		self._create_lv(size=(mib(2) + 13))

	def test_lv_create_thin_pool(self):
		self._create_lv(True)

	def test_lv_rename(self):
		# Rename a regular LV
		lv = self._create_lv()

		path = self.objs[MANAGER_INT][0].Manager.LookUpByLvmId(lv.LvCommon.Name)
		prev_path = path

		new_name = 'renamed_' + lv.LvCommon.Name

		self.handle_return(lv.Lv.Rename(dbus.String(new_name),
										dbus.Int32(g_tmo), EOD))

		path = self.objs[MANAGER_INT][0].Manager.LookUpByLvmId(
			dbus.String(new_name))

		self._check_consistency()
		self.assertTrue(prev_path == path, "%s != %s" % (prev_path, path))

	def test_lv_thinpool_rename(self):
		# Rename a thin pool
		tp = self._create_lv(True)
		self.assertTrue(
			THINPOOL_LV_PATH in tp.object_path,
			"%s" % (tp.object_path))

		new_name = 'renamed_' + tp.LvCommon.Name
		self.handle_return(tp.Lv.Rename(
			dbus.String(new_name), dbus.Int32(g_tmo), EOD))
		tp.update()
		self._check_consistency()
		self.assertEqual(new_name, tp.LvCommon.Name)

	# noinspection PyUnresolvedReferences
	def test_lv_on_thin_pool_rename(self):
		# Rename a LV on a thin Pool

		# This returns a LV with the LV interface, need to get a proxy for
		# thinpool interface too
		tp = self._create_lv(True)

		thin_path = self.handle_return(
			tp.ThinPool.LvCreate(
				dbus.String(lv_n('_thin_lv')),
				dbus.UInt64(mib(8)),
				dbus.Int32(g_tmo),
				EOD)
		)

		lv = ClientProxy(self.bus, thin_path,
							interfaces=(LV_COMMON_INT, LV_INT))

		rc = self.handle_return(
			lv.Lv.Rename(
				dbus.String('rename_test' + lv.LvCommon.Name),
				dbus.Int32(g_tmo),
				EOD)
		)

		self.assertTrue(rc == '/')
		self._check_consistency()

	def test_lv_remove(self):
		lv = self._create_lv().Lv
		rc = self.handle_return(
			lv.Remove(
				dbus.Int32(g_tmo),
				EOD))
		self.assertTrue(rc == '/')
		self._check_consistency()

	def test_lv_snapshot(self):
		lv_p = self._create_lv()
		ss_name = 'ss_' + lv_p.LvCommon.Name

		rc = self.handle_return(lv_p.Lv.Snapshot(
			dbus.String(ss_name),
			dbus.UInt64(0),
			dbus.Int32(g_tmo),
			EOD))

		self.assertTrue(rc != '/')

	# noinspection PyUnresolvedReferences
	def _wait_for_job(self, j_path):
		rc = None
		j = ClientProxy(self.bus, j_path, interfaces=(JOB_INT, )).Job

		while True:
			j.update()
			if j.Complete:
				(ec, error_msg) = j.GetError
				self.assertTrue(ec == 0, "%d :%s" % (ec, error_msg))

				if ec == 0:
					self.assertTrue(j.Percent == 100, "P= %f" % j.Percent)

				rc = j.Result
				j.Remove()

				break

			if j.Wait(1):
				j.update()
				self.assertTrue(j.Complete)

		return rc

	def test_lv_create_pv_specific(self):
		vg = self._vg_create().Vg

		pv = vg.Pvs

		pvp = ClientProxy(self.bus, pv[0], interfaces=(PV_INT,))

		self._test_lv_create(
			vg.LvCreate, (
				dbus.String(lv_n()),
				dbus.UInt64(mib(4)),
				dbus.Array([[pvp.object_path, 0, (pvp.Pv.PeCount - 1)]],
				signature='(ott)'),
				dbus.Int32(g_tmo), EOD), vg, LV_BASE_INT)

	def test_lv_resize(self):

		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg
		lv = self._create_lv(vg=vg, size=mib(16))

		for size in \
			[
			lv.LvCommon.SizeBytes + 4194304,
			lv.LvCommon.SizeBytes - 4194304,
			lv.LvCommon.SizeBytes + 2048,
			lv.LvCommon.SizeBytes - 2048]:

			pv_in_use = [i[0] for i in lv.LvCommon.Devices]
			# Select a PV in the VG that isn't in use
			pv_empty = [p for p in vg.Pvs if p not in pv_in_use]

			prev = lv.LvCommon.SizeBytes

			if len(pv_empty):
				p = ClientProxy(self.bus, pv_empty[0], interfaces=(PV_INT,))

				rc = self.handle_return(
					lv.Lv.Resize(
						dbus.UInt64(size),
						dbus.Array(
							[[p.object_path, 0, p.Pv.PeCount - 1]], '(oii)'),
						dbus.Int32(g_tmo), EOD))
			else:
				rc = self.handle_return(
					lv.Lv.Resize(
						dbus.UInt64(size),
						dbus.Array([], '(oii)'),
						dbus.Int32(g_tmo), EOD))

			self.assertEqual(rc, '/')
			self._check_consistency()

			lv.update()

			if prev < size:
				self.assertTrue(lv.LvCommon.SizeBytes > prev)
			else:
				# We are testing re-sizing to same size too...
				self.assertTrue(lv.LvCommon.SizeBytes <= prev)

	def test_lv_resize_same(self):
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg
		lv = self._create_lv(vg=vg)

		with self.assertRaises(dbus.exceptions.DBusException):
				lv.Lv.Resize(
					dbus.UInt64(lv.LvCommon.SizeBytes),
					dbus.Array([], '(oii)'),
					dbus.Int32(-1), EOD)

	def test_lv_move(self):
		lv = self._create_lv()

		pv_path_move = str(lv.LvCommon.Devices[0][0])

		# Test moving a specific LV
		rc = self.handle_return(
			lv.Lv.Move(
				dbus.ObjectPath(pv_path_move),
				dbus.Struct((0, 0), signature='(tt)'),
				dbus.Array([], '(ott)'), dbus.Int32(g_tmo),
				EOD))
		self.assertTrue(rc == '/')
		self._check_consistency()

		lv.update()
		new_pv = str(lv.LvCommon.Devices[0][0])
		self.assertTrue(
			pv_path_move != new_pv, "%s == %s" % (pv_path_move, new_pv))

	def test_lv_activate_deactivate(self):
		lv_p = self._create_lv()
		lv_p.update()

		self.handle_return(lv_p.Lv.Deactivate(
			dbus.UInt64(0), dbus.Int32(g_tmo), EOD))
		lv_p.update()
		self.assertFalse(lv_p.LvCommon.Active)
		self._check_consistency()

		self.handle_return(lv_p.Lv.Activate(
			dbus.UInt64(0), dbus.Int32(g_tmo), EOD))

		lv_p.update()
		self.assertTrue(lv_p.LvCommon.Active)
		self._check_consistency()

		# Try control flags
		for i in range(0, 5):

			self.handle_return(lv_p.Lv.Activate(
				dbus.UInt64(1 << i),
				dbus.Int32(g_tmo),
				EOD))

			self.assertTrue(lv_p.LvCommon.Active)
			self._check_consistency()

	def test_move(self):
		lv = self._create_lv()

		# Test moving without being LV specific
		vg = ClientProxy(self.bus, lv.LvCommon.Vg, interfaces=(VG_INT, )).Vg
		pv_to_move = str(lv.LvCommon.Devices[0][0])

		rc = self.handle_return(
			vg.Move(
				dbus.ObjectPath(pv_to_move),
				dbus.Struct((0, 0), signature='tt'),
				dbus.Array([], '(ott)'),
				dbus.Int32(0),
				EOD))
		self.assertEqual(rc, '/')
		self._check_consistency()

		vg.update()
		lv.update()

		location = lv.LvCommon.Devices[0][0]

		dst = None
		for p in vg.Pvs:
			if p != location:
				dst = p

		# Fetch the destination
		pv = ClientProxy(self.bus, dst, interfaces=(PV_INT, )).Pv

		# Test range, move it to the middle of the new destination
		job = self.handle_return(
			vg.Move(
				dbus.ObjectPath(location),
				dbus.Struct((0, 0), signature='tt'),
				dbus.Array([(dst, pv.PeCount / 2, 0), ], '(ott)'),
				dbus.Int32(g_tmo),
				EOD))
		self.assertEqual(job, '/')
		self._check_consistency()

	def test_job_handling(self):
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg_name = vg_n()

		# Test getting a job right away
		vg_path, vg_job = self.objs[MANAGER_INT][0].Manager.VgCreate(
			dbus.String(vg_name),
			dbus.Array(pv_paths, 'o'),
			dbus.Int32(0),
			EOD)

		self.assertTrue(vg_path == '/')
		self.assertTrue(vg_job and len(vg_job) > 0)

		self._wait_for_job(vg_job)

	def _test_expired_timer(self, num_lvs):
		rc = False
		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		# In small configurations lvm is pretty snappy, so lets create a VG
		# add a number of LVs and then remove the VG and all the contained
		# LVs which appears to consistently run a little slow.

		vg_proxy = self._vg_create(pv_paths)

		for i in range(0, num_lvs):

			vg_proxy.update()
			if vg_proxy.Vg.FreeCount > 0:
				job = self.handle_return(
					vg_proxy.Vg.LvCreateLinear(
						dbus.String(lv_n()),
						dbus.UInt64(mib(4)),
						dbus.Boolean(False),
						dbus.Int32(g_tmo),
						EOD))
				self.assertTrue(job != '/')
			else:
				# We ran out of space, test will probably fail
				break

		# Make sure that we are honoring the timeout
		start = time.time()

		remove_job = vg_proxy.Vg.Remove(dbus.Int32(1), EOD)

		end = time.time()

		tt_remove = float(end) - float(start)

		self.assertTrue(tt_remove < 2.0, "remove time %s" % (str(tt_remove)))

		# Depending on how long it took we could finish either way
		if remove_job != '/':
			# We got a job
			result = self._wait_for_job(remove_job)
			self.assertTrue(result == '/')
			rc = True
		else:
			# It completed before timer popped
			pass

		return rc

	def test_job_handling_timer(self):

		yes = False

		for pp in self.objs[PV_INT]:
			if '/dev/sd' not in pp.Pv.Name:
				std_err_print("Skipping test_job_handling_timer on loopback")
				return

		# This may not pass
		for i in [48, 64, 128]:
			yes = self._test_expired_timer(i)
			if yes:
				break
			std_err_print('Attempt (%d) failed, trying again...' % (i))

		self.assertTrue(yes)

	def test_pv_tags(self):
		pvs = []

		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg

		# Get the PVs
		for p in vg.Pvs:
			pvs.append(ClientProxy(self.bus, p, interfaces=(PV_INT, )).Pv)

		for tags_value in [['hello'], ['foo', 'bar']]:

			rc = self.handle_return(
				vg.PvTagsAdd(
					dbus.Array(vg.Pvs, 'o'),
					dbus.Array(tags_value, 's'),
					dbus.Int32(g_tmo),
					EOD))
			self.assertTrue(rc == '/')

			for p in pvs:
				p.update()
				self.assertTrue(sorted(tags_value) == p.Tags)

			rc = self.handle_return(
				vg.PvTagsDel(
					dbus.Array(vg.Pvs, 'o'),
					dbus.Array(tags_value, 's'),
					dbus.Int32(g_tmo),
					EOD))
			self.assertEqual(rc, '/')

			for p in pvs:
				p.update()
				self.assertTrue([] == p.Tags)

	def test_vg_tags(self):
		vg = self._vg_create().Vg

		t = ['Testing', 'tags']

		self.handle_return(
			vg.TagsAdd(
				dbus.Array(t, 's'),
				dbus.Int32(g_tmo),
				EOD))

		vg.update()
		self.assertTrue(t == vg.Tags)

		self.handle_return(
			vg.TagsDel(
				dbus.Array(t, 's'),
				dbus.Int32(g_tmo),
				EOD))
		vg.update()
		self.assertTrue([] == vg.Tags)

	def test_lv_tags(self):
		vg = self._vg_create().Vg
		lv = self._test_lv_create(
			vg.LvCreateLinear,
			(dbus.String(lv_n()),
			dbus.UInt64(mib(4)),
			dbus.Boolean(False),
			dbus.Int32(g_tmo),
			EOD),
			vg, LV_BASE_INT)

		t = ['Testing', 'tags']

		self.handle_return(
			lv.Lv.TagsAdd(
				dbus.Array(t, 's'), dbus.Int32(g_tmo), EOD))
		lv.update()
		self.assertTrue(t == lv.LvCommon.Tags)

		self.handle_return(
			lv.Lv.TagsDel(
				dbus.Array(t, 's'),
				dbus.Int32(g_tmo),
				EOD))
		lv.update()
		self.assertTrue([] == lv.LvCommon.Tags)

	def test_vg_allocation_policy_set(self):
		vg = self._vg_create().Vg

		for p in ['anywhere', 'contiguous', 'cling', 'normal']:
			rc = self.handle_return(
				vg.AllocationPolicySet(
					dbus.String(p), dbus.Int32(g_tmo), EOD))

			self.assertEqual(rc, '/')
			vg.update()

			prop = getattr(vg, 'Alloc' + p.title())
			self.assertTrue(prop)

	def test_vg_max_pv(self):
		vg = self._vg_create().Vg

		# BZ: https://bugzilla.redhat.com/show_bug.cgi?id=1280496
		# TODO: Add a test back for larger values here when bug is resolved
		for p in [0, 1, 10, 100, 100, 1024, 2 ** 32 - 1]:
			rc = self.handle_return(
				vg.MaxPvSet(
					dbus.UInt64(p), dbus.Int32(g_tmo), EOD))
			self.assertEqual(rc, '/')
			vg.update()
			self.assertTrue(
				vg.MaxPv == p,
				"Expected %s != Actual %s" % (str(p), str(vg.MaxPv)))

	def test_vg_max_lv(self):
		vg = self._vg_create().Vg

		# BZ: https://bugzilla.redhat.com/show_bug.cgi?id=1280496
		# TODO: Add a test back for larger values here when bug is resolved
		for p in [0, 1, 10, 100, 100, 1024, 2 ** 32 - 1]:
			rc = self.handle_return(
				vg.MaxLvSet(
					dbus.UInt64(p), dbus.Int32(g_tmo), EOD))
			self.assertEqual(rc, '/')
			vg.update()
			self.assertTrue(
				vg.MaxLv == p,
				"Expected %s != Actual %s" % (str(p), str(vg.MaxLv)))

	def test_vg_uuid_gen(self):
		vg = self._vg_create().Vg
		prev_uuid = vg.Uuid
		rc = self.handle_return(
			vg.UuidGenerate(
				dbus.Int32(g_tmo),
				EOD))
		self.assertEqual(rc, '/')
		vg.update()
		self.assertTrue(
			vg.Uuid != prev_uuid,
			"Expected %s != Actual %s" % (vg.Uuid, prev_uuid))

	def test_vg_activate_deactivate(self):
		vg = self._vg_create().Vg
		self._test_lv_create(
			vg.LvCreateLinear, (
				dbus.String(lv_n()),
				dbus.UInt64(mib(4)),
				dbus.Boolean(False),
				dbus.Int32(g_tmo),
				EOD),
			vg, LV_BASE_INT)

		vg.update()

		rc = self.handle_return(
			vg.Deactivate(
				dbus.UInt64(0), dbus.Int32(g_tmo), EOD))
		self.assertEqual(rc, '/')
		self._check_consistency()

		rc = self.handle_return(
			vg.Activate(
				dbus.UInt64(0), dbus.Int32(g_tmo), EOD))

		self.assertEqual(rc, '/')
		self._check_consistency()

		# Try control flags
		for i in range(0, 5):
			self.handle_return(
				vg.Activate(
					dbus.UInt64(1 << i),
					dbus.Int32(g_tmo),
					EOD))

	def test_pv_resize(self):

		self.assertTrue(len(self.objs[PV_INT]) > 0)

		if len(self.objs[PV_INT]) > 0:
			pv = ClientProxy(self.bus, self.objs[PV_INT][0].object_path,
								interfaces=(PV_INT, )).Pv

			original_size = pv.SizeBytes

			new_size = original_size / 2

			self.handle_return(
				pv.ReSize(
					dbus.UInt64(new_size),
					dbus.Int32(g_tmo),
					EOD))

			self._check_consistency()
			pv.update()

			self.assertTrue(pv.SizeBytes != original_size)
			self.handle_return(
				pv.ReSize(
					dbus.UInt64(0),
					dbus.Int32(g_tmo),
					EOD))
			self._check_consistency()
			pv.update()
			self.assertTrue(pv.SizeBytes == original_size)

	def test_pv_allocation(self):

		pv_paths = []
		for pp in self.objs[PV_INT]:
			pv_paths.append(pp.object_path)

		vg = self._vg_create(pv_paths).Vg

		pv = ClientProxy(self.bus, vg.Pvs[0], interfaces=(PV_INT, )).Pv

		self.handle_return(
			pv.AllocationEnabled(
				dbus.Boolean(False),
				dbus.Int32(g_tmo),
				EOD))

		pv.update()
		self.assertFalse(pv.Allocatable)

		self.handle_return(
			pv.AllocationEnabled(
				dbus.Boolean(True),
				dbus.Int32(g_tmo),
				EOD))

		self.handle_return(
			pv.AllocationEnabled(
				dbus.Boolean(True),
				dbus.Int32(g_tmo),
				EOD))
		pv.update()
		self.assertTrue(pv.Allocatable)

		self._check_consistency()

	@staticmethod
	def _get_devices():
		context = pyudev.Context()
		return context.list_devices(subsystem='block', MAJOR='8')

	def test_pv_scan(self):
		devices = TestDbusService._get_devices()

		mgr = self._manager().Manager

		self.assertEqual(
			self.handle_return(
				mgr.PvScan(
					dbus.Boolean(False),
					dbus.Boolean(True),
					dbus.Array([], 's'),
					dbus.Array([], '(ii)'),
					dbus.Int32(g_tmo),
					EOD)), '/')

		self._check_consistency()
		self.assertEqual(
			self.handle_return(
				mgr.PvScan(
					dbus.Boolean(False),
					dbus.Boolean(False),
					dbus.Array([], 's'),
					dbus.Array([], '(ii)'),
					dbus.Int32(g_tmo),
					EOD)), '/')

		self._check_consistency()

		block_path = []
		for d in devices:
			block_path.append(d.properties['DEVNAME'])

		self.assertEqual(
			self.handle_return(
				mgr.PvScan(
					dbus.Boolean(False),
					dbus.Boolean(True),
					dbus.Array(block_path, 's'),
					dbus.Array([], '(ii)'),
					dbus.Int32(g_tmo),
					EOD)), '/')

		self._check_consistency()

		mm = []
		for d in devices:
			mm.append((int(d.properties['MAJOR']), int(d.properties['MINOR'])))

		self.assertEqual(
			self.handle_return(
				mgr.PvScan(
					dbus.Boolean(False),
					dbus.Boolean(True),
					dbus.Array(block_path, 's'),
					dbus.Array(mm, '(ii)'),
					dbus.Int32(g_tmo),
					EOD)), '/')

		self._check_consistency()

		self.assertEqual(
			self.handle_return(
				mgr.PvScan(
					dbus.Boolean(False),
					dbus.Boolean(True),
					dbus.Array([], 's'),
					dbus.Array(mm, '(ii)'),
					dbus.Int32(g_tmo),
					EOD)), '/')
		self._check_consistency()

	@staticmethod
	def _write_some_data(device_path, size):
		blocks = int(size / 512)
		block = bytearray(512)
		for i in range(0, 512):
			block[i] = i % 255

		with open(device_path, mode='wb') as lv:
			for i in range(0, blocks):
				lv.write(block)

	def test_snapshot_merge(self):
		# Create a non-thin LV and merge it
		ss_size = mib(8)

		lv_p = self._create_lv(size=mib(16))
		ss_name = lv_p.LvCommon.Name + '_snap'

		snapshot_path = self.handle_return(
			lv_p.Lv.Snapshot(
				dbus.String(ss_name),
				dbus.UInt64(ss_size),
				dbus.Int32(g_tmo),
				EOD))

		ss = ClientProxy(self.bus, snapshot_path,
							interfaces=(LV_COMMON_INT, LV_INT, SNAPSHOT_INT, ))

		# Write some data to snapshot so merge takes some time
		TestDbusService._write_some_data(ss.LvCommon.Path, ss_size / 2)

		job_path = self.handle_return(
			ss.Snapshot.Merge(
				dbus.Int32(g_tmo),
				EOD))
		self.assertEqual(job_path, '/')

	def test_snapshot_merge_thin(self):
		# Create a thin LV, snapshot it and merge it
		tp = self._create_lv(True)

		thin_path = self.handle_return(
			tp.ThinPool.LvCreate(
				dbus.String(lv_n('_thin_lv')),
				dbus.UInt64(mib(10)),
				dbus.Int32(g_tmo),
				EOD))

		lv_p = ClientProxy(self.bus, thin_path,
							interfaces=(LV_INT, LV_COMMON_INT))

		ss_name = lv_p.LvCommon.Name + '_snap'
		snapshot_path = self.handle_return(
			lv_p.Lv.Snapshot(
				dbus.String(ss_name),
				dbus.UInt64(0),
				dbus.Int32(g_tmo),
				EOD))

		ss = ClientProxy(self.bus, snapshot_path,
							interfaces=(LV_INT, LV_COMMON_INT, SNAPSHOT_INT))

		job_path = self.handle_return(
			ss.Snapshot.Merge(
				dbus.Int32(g_tmo), EOD)
		)
		self.assertTrue(job_path == '/')

	def _create_cache_pool(self, vg=None):

		if not vg:
			vg = self._vg_create().Vg

		md = self._create_lv(size=(mib(8)), vg=vg)
		data = self._create_lv(size=(mib(8)), vg=vg)

		cache_pool_path = self.handle_return(
			vg.CreateCachePool(
				dbus.ObjectPath(md.object_path),
				dbus.ObjectPath(data.object_path),
				dbus.Int32(g_tmo),
				EOD))

		cp = ClientProxy(self.bus, cache_pool_path,
							interfaces=(CACHE_POOL_INT, ))

		return vg, cp

	def test_cache_pool_create(self):

		vg, cache_pool = self._create_cache_pool()

		self.assertTrue(
			'/com/redhat/lvmdbus1/CachePool' in cache_pool.object_path)

	def test_cache_lv_create(self):

		for destroy_cache in [True, False]:
			vg, cache_pool = self._create_cache_pool()

			lv_to_cache = self._create_lv(size=mib(8), vg=vg)

			c_lv_path = self.handle_return(
				cache_pool.CachePool.CacheLv(
					dbus.ObjectPath(lv_to_cache.object_path),
					dbus.Int32(g_tmo),
					EOD))

			cached_lv = ClientProxy(self.bus, c_lv_path,
									interfaces=(LV_COMMON_INT, LV_INT,
												CACHE_LV_INT))

			uncached_lv_path = self.handle_return(
				cached_lv.CachedLv.DetachCachePool(
					dbus.Boolean(destroy_cache),
					dbus.Int32(g_tmo),
					EOD))

			self.assertTrue(
				'/com/redhat/lvmdbus1/Lv' in uncached_lv_path)

			rc = self.handle_return(
				vg.Remove(dbus.Int32(g_tmo), EOD))
			self.assertTrue(rc == '/')

	def test_vg_change(self):
		vg_proxy = self._vg_create()

		result = self.handle_return(vg_proxy.Vg.Change(
			dbus.Int32(g_tmo),
			dbus.Dictionary({'-a': 'ay'}, 'sv')))
		self.assertTrue(result == '/')

		result = self.handle_return(
			vg_proxy.Vg.Change(
				dbus.Int32(g_tmo),
				dbus.Dictionary({'-a': 'n'}, 'sv')))
		self.assertTrue(result == '/')

	@staticmethod
	def _invalid_vg_lv_name_characters():
		bad_vg_lv_set = set(string.printable) - \
			set(string.ascii_letters + string.digits + '.-_+')
		return ''.join(bad_vg_lv_set)

	def test_invalid_names(self):
		mgr = self.objs[MANAGER_INT][0].Manager

		# Pv device path
		with self.assertRaises(dbus.exceptions.DBusException):
			self.handle_return(
				mgr.PvCreate(
					dbus.String("/dev/space in name"),
					dbus.Int32(g_tmo),
					EOD))

		# VG Name testing...
		# Go through all bad characters
		pv_paths = [self.objs[PV_INT][0].object_path]
		bad_chars = TestDbusService._invalid_vg_lv_name_characters()
		for c in bad_chars:
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					mgr.VgCreate(
						dbus.String("name%s" % (c)),
						dbus.Array(pv_paths, 'o'),
						dbus.Int32(g_tmo),
						EOD))

		# Bad names
		for bad in [".", ".."]:
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					mgr.VgCreate(
						dbus.String(bad),
						dbus.Array(pv_paths, 'o'),
						dbus.Int32(g_tmo),
						EOD))

		# Exceed name length
		for i in [128, 1024, 4096]:
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					mgr.VgCreate(
						dbus.String('T' * i),
						dbus.Array(pv_paths, 'o'),
						dbus.Int32(g_tmo),
						EOD))

		# Create a VG and try to create LVs with different bad names
		vg_path = self.handle_return(
			mgr.VgCreate(
				dbus.String(vg_n()),
				dbus.Array(pv_paths, 'o'),
				dbus.Int32(g_tmo),
				EOD))

		vg_proxy = ClientProxy(self.bus, vg_path, interfaces=(VG_INT, ))

		for c in bad_chars:
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					vg_proxy.Vg.LvCreateLinear(
						dbus.String(lv_n() + c),
						dbus.UInt64(mib(4)),
						dbus.Boolean(False),
						dbus.Int32(g_tmo),
						EOD))

		for reserved in (
				"_cdata", "_cmeta", "_corig", "_mimage", "_mlog",
				"_pmspare", "_rimage", "_rmeta", "_tdata", "_tmeta",
				"_vorigin"):
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					vg_proxy.Vg.LvCreateLinear(
						dbus.String(lv_n() + reserved),
						dbus.UInt64(mib(4)),
						dbus.Boolean(False),
						dbus.Int32(g_tmo),
						EOD))

		for reserved in ("snapshot", "pvmove"):
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					vg_proxy.Vg.LvCreateLinear(
						dbus.String(reserved + lv_n()),
						dbus.UInt64(mib(4)),
						dbus.Boolean(False),
						dbus.Int32(g_tmo),
						EOD))

	_ALLOWABLE_TAG_CH = string.ascii_letters + string.digits + "._-+/=!:&#"

	def _invalid_tag_characters(self):
		bad_tag_ch_set = set(string.printable) - set(self._ALLOWABLE_TAG_CH)
		return ''.join(bad_tag_ch_set)

	def test_invalid_tags(self):
		mgr = self.objs[MANAGER_INT][0].Manager
		pv_paths = [self.objs[PV_INT][0].object_path]

		vg_path = self.handle_return(
			mgr.VgCreate(
				dbus.String(vg_n()),
				dbus.Array(pv_paths, 'o'),
				dbus.Int32(g_tmo),
				EOD))
		vg_proxy = ClientProxy(self.bus, vg_path, interfaces=(VG_INT, ))

		for c in self._invalid_tag_characters():
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					vg_proxy.Vg.TagsAdd(
						dbus.Array([c], 's'),
						dbus.Int32(g_tmo),
						EOD))

		for c in self._invalid_tag_characters():
			with self.assertRaises(dbus.exceptions.DBusException):
				self.handle_return(
					vg_proxy.Vg.TagsAdd(
						dbus.Array(["a%sb" % (c)], 's'),
						dbus.Int32(g_tmo),
						EOD))

	def test_tag_names(self):
		mgr = self.objs[MANAGER_INT][0].Manager
		pv_paths = [self.objs[PV_INT][0].object_path]

		vg_path = self.handle_return(
			mgr.VgCreate(
				dbus.String(vg_n()),
				dbus.Array(pv_paths, 'o'),
				dbus.Int32(g_tmo),
				EOD))
		vg_proxy = ClientProxy(self.bus, vg_path, interfaces=(VG_INT, ))

		for i in range(1, 64):
			tag = rs(i, "", self._ALLOWABLE_TAG_CH)

			tmp = self.handle_return(
				vg_proxy.Vg.TagsAdd(
					dbus.Array([tag], 's'),
					dbus.Int32(g_tmo),
					EOD))
			self.assertTrue(tmp == '/')
			vg_proxy.update()

			self.assertTrue(
				tag in vg_proxy.Vg.Tags,
				"%s not in %s" % (tag, str(vg_proxy.Vg.Tags)))

			self.assertEqual(
				i, len(vg_proxy.Vg.Tags),
				"%d != %d" % (i, len(vg_proxy.Vg.Tags)))

	def test_tag_regression(self):
		mgr = self.objs[MANAGER_INT][0].Manager
		pv_paths = [self.objs[PV_INT][0].object_path]

		vg_path = self.handle_return(
			mgr.VgCreate(
				dbus.String(vg_n()),
				dbus.Array(pv_paths, 'o'),
				dbus.Int32(g_tmo),
				EOD))
		vg_proxy = ClientProxy(self.bus, vg_path, interfaces=(VG_INT, ))

		tag = '--h/K.6g0A4FOEatf3+k_nI/Yp&L_u2oy-=j649x:+dUcYWPEo6.IWT0c'

		tmp = self.handle_return(
			vg_proxy.Vg.TagsAdd(
				dbus.Array([tag], 's'),
				dbus.Int32(g_tmo),
				EOD))
		self.assertTrue(tmp == '/')
		vg_proxy.update()

		self.assertTrue(
			tag in vg_proxy.Vg.Tags,
			"%s not in %s" % (tag, str(vg_proxy.Vg.Tags)))


class AggregateResults(object):

	def __init__(self):
		self.no_errors = True

	def register_result(self, result):
		if not result.result.wasSuccessful():
			self.no_errors = False

	def register_fail(self):
		self.no_errors = False

	def exit_run(self):
		if self.no_errors:
			sys.exit(0)
		sys.exit(1)


if __name__ == '__main__':

	r = AggregateResults()
	mode = int(test_shell)

	if mode == 0:
		std_err_print('\n*** Testing only lvm fork & exec test mode ***\n')
	elif mode == 1:
		std_err_print('\n*** Testing fork & exec & lvm shell mode ***\n')
	else:
		std_err_print('\n*** Testing only lvm shell mode ***\n')

	for g_tmo in [0, 15]:
		if mode == 0:
			if set_execution(False, r):
				r.register_result(unittest.main(exit=False))
		elif mode == 2:
			if set_execution(True, r):
				r.register_result(unittest.main(exit=False))
		else:
			if set_execution(False, r):
				r.register_result(unittest.main(exit=False))
			# Test lvm shell
			if set_execution(True, r):
				r.register_result(unittest.main(exit=False))

		if not r.no_errors:
			break

	r.exit_run()
