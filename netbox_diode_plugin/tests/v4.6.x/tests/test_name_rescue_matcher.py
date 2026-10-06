"""Tests for the last-resort, unscoped name-rescue matcher."""
from unittest import mock

from django.test import TestCase
from ipam.models import VRF
from tenancy.models import Tenant, TenantGroup

from netbox_diode_plugin.api import matcher as matcher_mod
from netbox_diode_plugin.api.matcher import (
    NameRescueMatcher,
    find_existing_object,
    get_model_matchers,
)


def _config(overrides):
    """Return a get_plugin_config stand-in that serves overrides, else the real value."""
    real = matcher_mod.get_plugin_config

    def _get(plugin, key, *args, **kwargs):
        if key in overrides:
            return overrides[key]
        return real(plugin, key, *args, **kwargs)

    return mock.patch.object(matcher_mod, "get_plugin_config", side_effect=_get)


class NameRescueRegistrationTests(TestCase):
    """The rescue matcher is appended last, and only when enabled."""

    def test_rescue_matcher_is_last_for_vrf(self):
        """With the default config the VRF rescue runs after every other criterion."""
        matchers = get_model_matchers(VRF)
        self.assertIsInstance(matchers[-1], NameRescueMatcher)
        self.assertEqual(sum(isinstance(m, NameRescueMatcher) for m in matchers), 1)

    def test_disabled_setting_removes_rescue(self):
        """match_name_rescue=False restores the strictly scoped behaviour."""
        with _config({"match_name_rescue": False}):
            matchers = get_model_matchers(VRF)
        self.assertFalse(any(isinstance(m, NameRescueMatcher) for m in matchers))

    def test_scopes_override_can_opt_a_type_out(self):
        """Mapping a type to None in match_name_rescue_scopes removes its rescue."""
        with _config({"match_name_rescue_scopes": {"ipam.vrf": None}}):
            matchers = get_model_matchers(VRF)
        self.assertFalse(any(isinstance(m, NameRescueMatcher) for m in matchers))

    def test_scopes_override_without_relaxed_list_relaxes_every_gate(self):
        """A two-element override spec relaxes every gated field on the row."""
        override = {"ipam.vrf": [["name"], ["rd", "tenant"]]}
        with _config({"match_name_rescue_scopes": override}):
            rescue = [m for m in get_model_matchers(VRF) if isinstance(m, NameRescueMatcher)]
        self.assertEqual(len(rescue), 1)
        self.assertEqual(rescue[0].row_required_null_fields, ())

    def test_type_without_rescue_scope_gets_none(self):
        """Types outside the built-in table get no rescue matcher."""
        from dcim.models import Site

        self.assertFalse(
            any(isinstance(m, NameRescueMatcher) for m in get_model_matchers(Site))
        )


class NameRescueMatchingTests(TestCase):
    """Name-only payloads bind a uniquely named, scoped row and decline otherwise."""

    @classmethod
    def setUpTestData(cls):
        """Seed tenanted VRFs, an rd-bearing VRF, and a grouped tenant."""
        cls.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        cls.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        cls.vrf_a = VRF.objects.create(name="VRF-A", tenant=cls.tenant_a)
        VRF.objects.create(name="VRF-DUP", tenant=cls.tenant_a)
        VRF.objects.create(name="VRF-DUP", tenant=cls.tenant_b)
        VRF.objects.create(name="VRF-RD", rd="65000:1")
        group = TenantGroup.objects.create(name="Group A", slug="group-a")
        cls.tenant_grouped = Tenant.objects.create(
            name="Tenant C", slug="tenant-c", group=group
        )

    def _find(self, data, object_type):
        with _config({"find_obj_cache_ttl": 0}):
            return find_existing_object(data, object_type)

    def test_name_only_vrf_binds_tenanted_row(self):
        """A VRF sent without rd or tenant matches the one tenanted row of that name."""
        self.assertEqual(self._find({"name": "VRF-A"}, "ipam.vrf"), self.vrf_a)

    def test_ambiguous_name_declines(self):
        """Two same-named VRFs in different tenants: no guess, no match."""
        self.assertIsNone(self._find({"name": "VRF-DUP"}, "ipam.vrf"))

    def test_rd_bearing_row_is_not_rescued(self):
        """The rd is identity: an rd-less payload never folds into an rd-bearing VRF."""
        self.assertIsNone(self._find({"name": "VRF-RD"}, "ipam.vrf"))

    def test_scoped_payload_skips_rescue(self):
        """A payload that carries a tenant is left to the real criteria."""
        rescue = [m for m in get_model_matchers(VRF) if isinstance(m, NameRescueMatcher)][0]
        self.assertFalse(rescue.has_required_fields({"name": "VRF-A", "tenant": self.tenant_b}))

    def test_name_only_tenant_binds_grouped_row(self):
        """A tenant sent without a group matches the grouped tenant of that name."""
        self.assertEqual(
            self._find({"name": "Tenant C"}, "tenancy.tenant"), self.tenant_grouped
        )

    def test_disabled_rescue_does_not_match(self):
        """With the rescue off, the tenanted VRF is invisible to a name-only payload."""
        with _config({"match_name_rescue": False, "find_obj_cache_ttl": 0}):
            self.assertIsNone(find_existing_object({"name": "VRF-A"}, "ipam.vrf"))

    def test_rescue_has_no_fingerprint(self):
        """A bare name is too weak to fuse two batch entries on."""
        rescue = NameRescueMatcher(model_class=VRF, name="x", scope_fields=("rd", "tenant"))
        self.assertIsNone(rescue.fingerprint({"name": "VRF-A"}))
