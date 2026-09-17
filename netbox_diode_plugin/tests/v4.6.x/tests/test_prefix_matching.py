"""An ipam.prefix reference against a prefix NetBox already holds.

ipam.prefix has no unique constraint on prefix or on (prefix, vrf), so matching
rests entirely on the two logical criteria, and a miss silently produces a second
row rather than an IntegrityError. Worth covering directly, and worth covering
against a prefix created outside Diode -- an operator or a bootstrap script --
rather than only against one a previous ingest created.

Also pins that a more specific prefix inside an existing one is created as its
own row and leaves the covering prefix alone.
"""
from core.models import ObjectType
from extras.models import CustomField, CustomFieldChoiceSet
from ipam.models import VRF, Prefix
from django.test import TestCase
from tenancy.models import Tenant, TenantGroup

from netbox_diode_plugin.api.applier import apply_changeset
from netbox_diode_plugin.api.common import ChangeType
from netbox_diode_plugin.api.differ import generate_changeset


class PrefixDuplicateTestCase(TestCase):
    """An operator-created prefix, then an ingest naming the same CIDR + VRF."""

    @classmethod
    def setUpTestData(cls):
        cls.group = TenantGroup.objects.create(
            name="Example Labs", slug="example-labs"
        )
        cls.tenant = Tenant.objects.create(
            name="Tenant A", slug="tenant-a", group=cls.group
        )
        cls.vrf = VRF.objects.create(name="VRF-A", tenant=cls.tenant)

        types = [ObjectType.objects.get_for_model(m) for m in (Prefix,)]
        choices = CustomFieldChoiceSet.objects.create(
            name="example-choices", extra_choices=[["a1", "a1"]]
        )
        cls.cf_select = CustomField.objects.create(
            name="example_select", type="select", choice_set=choices, required=False
        )
        cls.cf_select.object_types.set(types)
        cls.cf_text = CustomField.objects.create(
            name="discovery_agent", type="text", required=False
        )
        cls.cf_text.object_types.set(types)

        # the prefix an operator (or the bootstrap script) already made
        cls.prefix = Prefix.objects.create(
            prefix="10.0.0.0/16", vrf=cls.vrf, tenant=cls.tenant
        )

    def _plan(self, entity, object_type="ipam.prefix"):
        return generate_changeset(entity, object_type).change_set

    def _apply(self, cs):
        """apply_changeset rejects an empty changeset; a NOOP plan is success."""
        if not cs.changes:
            return None
        return apply_changeset(cs, request=None)

    def _creates(self, cs, object_type="ipam.prefix"):
        return [c for c in cs.changes
                if c.object_type == object_type
                and c.change_type == ChangeType.CREATE]

    def _entity(self, **extra):
        entity = {
            "prefix": "10.0.0.0/16",
            "vrf": {"name": "VRF-A",
                    "tenant": {"name": "Tenant A",
                               "group": {"name": "Example Labs"}}},
            "tenant": {"name": "Tenant A", "group": {"name": "Example Labs"}},
        }
        entity.update(extra)
        return entity

    def test_bare_reingest_of_operator_prefix(self):
        """No custom fields: does the plain reference bind the existing prefix?"""
        cs = self._plan(self._entity())
        creates = self._creates(cs)
        self.assertEqual(creates, [], [c.to_dict() for c in creates])
        self._apply(cs)
        self.assertEqual(Prefix.objects.filter(prefix="10.0.0.0/16").count(), 1)

    def test_reingest_with_custom_fields(self):
        """With the producer's custom fields attached, as a producer sends them."""
        cs = self._plan(self._entity(custom_fields={
            "example_select": {"text": "a1"},
            "discovery_agent": {"text": "the producer"},
        }))
        creates = self._creates(cs)
        self.assertEqual(creates, [], [c.to_dict() for c in creates])
        self._apply(cs)
        self.assertEqual(Prefix.objects.filter(prefix="10.0.0.0/16").count(), 1)

    def test_more_specific_prefix_is_created_not_merged(self):
        """The /24 under the /16 must become its own prefix, not touch the /16."""
        cs = self._plan(self._entity(prefix="10.0.8.0/24"))
        self._apply(cs)
        self.assertEqual(Prefix.objects.filter(prefix="10.0.8.0/24").count(), 1)
        self.assertEqual(Prefix.objects.filter(prefix="10.0.0.0/16").count(), 1)

    def test_two_ingest_cycles_with_custom_fields_converge(self):
        """Scan twice. The second must plan nothing and add no second prefix."""
        # A fresh entity per scan: the transformer pops values out of the dict
        # it is given (_pop_custom_field_type_and_value), so a reused dict is
        # empty the second time round. A producer builds each one from the wire.
        def scan():
            return self._entity(custom_fields={
                "example_select": {"text": "a1"},
                "discovery_agent": {"text": "the producer"},
            })
        self._apply(self._plan(scan()))
        cs = self._plan(scan())
        non_noop = [c for c in cs.changes if c.change_type != ChangeType.NOOP]
        self.assertEqual(non_noop, [], [c.to_dict() for c in non_noop])
        self.assertEqual(Prefix.objects.filter(prefix="10.0.0.0/16").count(), 1)
