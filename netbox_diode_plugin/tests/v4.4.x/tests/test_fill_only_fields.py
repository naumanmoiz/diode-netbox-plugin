"""Tests for the fill_only_fields plugin setting."""
from unittest import mock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Platform, Site
from django.test import SimpleTestCase, TestCase
from ipam.models import IPAddress
from tenancy.models import Tenant

from netbox_diode_plugin.api import differ
from netbox_diode_plugin.api.common import ChangeType
from netbox_diode_plugin.api.differ import (
    apply_fill_only_fields,
    generate_changeset,
    get_fill_only_fields,
)


def _fill_only(config):
    """Serve ``config`` as fill_only_fields, every other setting unchanged."""
    real = differ.get_plugin_config

    def _get(plugin, key, *args, **kwargs):
        if key == "fill_only_fields":
            return config
        return real(plugin, key, *args, **kwargs)

    return mock.patch.object(differ, "get_plugin_config", side_effect=_get)


class FillOnlyConfigTests(SimpleTestCase):
    """Resolution of the setting into a per-model field set."""

    def test_default_is_empty(self):
        """The shipped default configures nothing."""
        self.assertEqual(get_fill_only_fields("ipam.ipaddress"), frozenset())

    def test_wildcard_applies_to_every_model_and_is_extended(self):
        """'*' applies everywhere; a model's own entry adds to it."""
        config = {"*": ["description"], "dcim.device": ["platform"]}
        with _fill_only(config):
            self.assertEqual(get_fill_only_fields("dcim.device"), {"description", "platform"})
            self.assertEqual(get_fill_only_fields("ipam.ipaddress"), {"description"})

    def test_malformed_entries_are_ignored(self):
        """A non-dict setting, or a bare string instead of a list, is ignored."""
        with _fill_only(["description"]):
            self.assertEqual(get_fill_only_fields("dcim.device"), frozenset())
        with _fill_only({"dcim.device": "description"}):
            self.assertEqual(get_fill_only_fields("dcim.device"), frozenset())

    def test_helper_keeps_stored_and_fills_empty(self):
        """Non-empty stored values win; empty ones (None, "", [], {}) are filled."""
        prechange = {
            "id": 1, "description": "kept", "comments": "", "dns_name": None,
            "tenant": 5, "role": "", "tags": [], "custom_fields": {},
        }
        entity = {
            "description": "new", "comments": "c", "dns_name": "host.example.com",
            "tenant": "new_object:tenancy.tenant:abc", "role": "vip",
            "tags": [1], "custom_fields": {"x": 1},
        }
        fields = ["description", "comments", "dns_name", "tenant", "role", "tags", "custom_fields"]
        with _fill_only({"ipam.ipaddress": fields}):
            refs = apply_fill_only_fields(
                "ipam.ipaddress", prechange, entity, ["tenant", "vrf"]
            )
        self.assertEqual(entity["description"], "kept")
        self.assertEqual(entity["tenant"], 5)
        self.assertEqual(entity["comments"], "c")
        self.assertEqual(entity["dns_name"], "host.example.com")
        self.assertEqual(entity["role"], "vip")
        self.assertEqual(entity["tags"], [1])
        self.assertEqual(entity["custom_fields"], {"x": 1})
        # the tenant reference was dropped with the incoming value
        self.assertEqual(refs, ["vrf"])

    def test_helper_ignores_fields_not_sent(self):
        """A listed field absent from the payload is not added to it."""
        entity = {"status": "active"}
        with _fill_only({"ipam.ipaddress": ["description"]}):
            apply_fill_only_fields("ipam.ipaddress", {"id": 1, "description": "x"}, entity, [])
        self.assertNotIn("description", entity)


class FillOnlyChangesetTests(TestCase):
    """End to end through generate_changeset against stored objects."""

    @classmethod
    def setUpTestData(cls):
        """Seed tenants, a populated IP, an empty IP and a device with a platform."""
        cls.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        cls.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        cls.ip_full = IPAddress.objects.create(
            address="192.0.2.10/24",
            description="curated",
            dns_name="curated.example.com",
            status="active",
            tenant=cls.tenant_a,
        )
        cls.ip_empty = IPAddress.objects.create(address="192.0.2.20/24", status="active")

        site = Site.objects.create(name="Site A", slug="site-a")
        mfr = Manufacturer.objects.create(name="Manufacturer A", slug="manufacturer-a")
        dt = DeviceType.objects.create(manufacturer=mfr, model="Model A", slug="model-a")
        role = DeviceRole.objects.create(name="Role A", slug="role-a")
        cls.platform_a = Platform.objects.create(name="Platform A", slug="platform-a")
        Platform.objects.create(name="Platform B", slug="platform-b")
        cls.device = Device.objects.create(
            name="device-a", site=site, device_type=dt, role=role,
            platform=cls.platform_a, description="curated",
        )

    @staticmethod
    def _plan(entity, object_type):
        return generate_changeset(entity, object_type).change_set

    @staticmethod
    def _change_for(cs, object_type):
        changes = [c for c in cs.changes if c.object_type == object_type]
        return changes[0] if changes else None

    def _ip_entity(self, address, **extra):
        entity = {
            "address": address,
            "description": "from producer",
            "dns_name": "producer.example.com",
            "status": "reserved",
            "tenant": {"name": "Tenant B"},
        }
        entity.update(extra)
        return entity

    def test_default_config_overwrites(self):
        """With the default {} every submitted field overwrites, as before."""
        cs = self._plan(self._ip_entity("192.0.2.10/24"), "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        self.assertEqual(change.change_type, ChangeType.UPDATE)
        self.assertEqual(change.data["description"], "from producer")
        self.assertEqual(change.data["dns_name"], "producer.example.com")
        self.assertEqual(change.data["status"], "reserved")
        self.assertEqual(change.data["tenant"], self.tenant_b.pk)

    def test_stored_non_empty_values_are_kept(self):
        """Populated fill-only fields, FK included, produce no change at all."""
        config = {"ipam.ipaddress": ["description", "dns_name", "status", "tenant"]}
        with _fill_only(config):
            cs = self._plan(self._ip_entity("192.0.2.10/24"), "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        # nothing else changed, so the whole changeset collapses to empty
        self.assertIsNone(change, [c.to_dict() for c in cs.changes])

    def test_stored_non_empty_kept_while_other_fields_update(self):
        """Fill-only fields keep stored values inside an otherwise real UPDATE."""
        config = {"ipam.ipaddress": ["description", "tenant"]}
        with _fill_only(config):
            cs = self._plan(self._ip_entity("192.0.2.10/24"), "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        self.assertEqual(change.change_type, ChangeType.UPDATE)
        self.assertEqual(change.data["description"], "curated")
        self.assertEqual(change.data["tenant"], self.tenant_a.pk)
        self.assertEqual(change.data["dns_name"], "producer.example.com")
        self.assertEqual(change.data["status"], "reserved")

    def test_stored_empty_values_are_filled(self):
        """Empty fill-only fields, FK included, take the incoming value."""
        config = {"ipam.ipaddress": ["description", "dns_name", "tenant"]}
        with _fill_only(config):
            cs = self._plan(self._ip_entity("192.0.2.20/24"), "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        self.assertEqual(change.change_type, ChangeType.UPDATE)
        self.assertEqual(change.data["description"], "from producer")
        self.assertEqual(change.data["dns_name"], "producer.example.com")
        self.assertEqual(change.data["tenant"], self.tenant_b.pk)

    def test_fk_to_new_object_is_dropped_with_its_reference(self):
        """A kept FK no longer points at a not-yet-created object."""
        config = {"ipam.ipaddress": ["tenant"]}
        entity = self._ip_entity("192.0.2.10/24", tenant={"name": "Tenant New"})
        with _fill_only(config):
            cs = self._plan(entity, "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        self.assertEqual(change.data["tenant"], self.tenant_a.pk)
        self.assertNotIn("tenant", change.new_refs)

    def test_device_platform_fk_kept(self):
        """A reference field on another model: the stored platform is kept."""
        entity = {
            "name": "device-a",
            "site": {"name": "Site A"},
            "device_type": {"model": "Model A", "manufacturer": {"name": "Manufacturer A"}},
            "role": {"name": "Role A"},
            "platform": {"name": "Platform B"},
            "description": "from producer",
        }
        with _fill_only({"dcim.device": ["description", "platform"]}):
            cs = self._plan(dict(entity), "dcim.device")
        self.assertIsNone(self._change_for(cs, "dcim.device"))

        cs = self._plan(dict(entity), "dcim.device")
        change = self._change_for(cs, "dcim.device")
        self.assertEqual(change.change_type, ChangeType.UPDATE)
        self.assertNotEqual(change.data["platform"], self.platform_a.pk)

    def test_wildcard_key(self):
        """'*' protects a field on every model."""
        with _fill_only({"*": ["description"]}):
            cs = self._plan(
                self._ip_entity("192.0.2.10/24", status="active",
                                dns_name="curated.example.com", tenant={"name": "Tenant A"}),
                "ipam.ipaddress",
            )
        self.assertIsNone(self._change_for(cs, "ipam.ipaddress"))

    def test_create_is_unaffected(self):
        """A new object takes every submitted value regardless of the setting."""
        config = {"*": ["description", "dns_name", "status", "tenant"]}
        with _fill_only(config):
            cs = self._plan(self._ip_entity("198.51.100.5/24"), "ipam.ipaddress")
        change = self._change_for(cs, "ipam.ipaddress")
        self.assertEqual(change.change_type, ChangeType.CREATE)
        self.assertEqual(change.data["description"], "from producer")
        self.assertEqual(change.data["dns_name"], "producer.example.com")
        self.assertEqual(change.data["status"], "reserved")
        self.assertEqual(change.data["tenant"], self.tenant_b.pk)
