"""How a tenant reference resolves, and what happens to references that carry it.

tenancy.tenant has no logical matcher: it is matched from the model's own unique
constraints, plus AutoSlugMatcher. That last one matters more than it looks.
The transformer generates a slug for any reference that carries none, and
AutoSlugMatcher matches on that slug ALONE -- no name condition, no group
condition -- so it runs last and quietly rescues references the group-aware
criteria rejected.

Three outcomes follow for a reference that omits its group, and they are very
different from each other:

  rescued    the stored slug equals slugify(name), so the reference still binds
             the right tenant -- by coincidence, until someone renames it
  cascading  the stored slug differs, so nothing matches. A second tenant is
             created -- and any reference carrying that tenant (a VRF, say) now
             holds an UnresolvedReference, cannot be matched either, and is
             duplicated too, though its own shape is correct
  ambiguous  several tenants share the slug, so the reference binds the oldest
             and the group never enters the decision

The cascade is the interesting one: a correctly-shaped reference duplicating
because of a defect one level down in a reference it carries.
"""
from ipam.models import VRF, IPAddress, Prefix
from django.test import TestCase
from tenancy.models import Tenant, TenantGroup

from netbox_diode_plugin.api.applier import apply_changeset
from netbox_diode_plugin.api.common import ChangeType
from netbox_diode_plugin.api.differ import generate_changeset


class _IngestMixin:
    """Shared plan/apply helpers over the real changeset path."""

    def _prefix_entity(self, prefix, vrf, tenant):
        return {"prefix": prefix, "vrf": vrf, "tenant": tenant}

    def _plan(self, entity, object_type="ipam.prefix"):
        return generate_changeset(entity, object_type).change_set

    def _apply(self, cs):
        return apply_changeset(cs, request=None)

    def _ingest(self, entity, object_type="ipam.prefix"):
        self._apply(self._plan(entity, object_type))

    def _creates_of(self, cs, object_type):
        return [c for c in cs.changes
                if c.object_type == object_type
                and c.change_type == ChangeType.CREATE]


class AutoSlugRescueTestCase(_IngestMixin, TestCase):
    """1. The group-less reference is rescued by the auto-generated slug."""

    @classmethod
    def setUpTestData(cls):
        """A grouped tenant whose slug IS slugify(name) -- the common case."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)
        cls.corp_core = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)

    def test_groupless_tenant_reference_still_binds_the_grouped_tenant(self):
        """Contradicts the producer: the group-less reference DOES match.

        Matchers 1-4 all decline (they need `group`, or restrict to group
        IS NULL). unique_autoslug_slug then matches on slug="corp" with no
        group condition, and finds the grouped tenant.
        """
        entity = self._prefix_entity(
            "10.1.0.0/24",
            vrf={"name": "CORP-CORE", "tenant": {"name": "Corp"}},
            tenant={"name": "Corp"},
        )
        cs = self._plan(entity)

        self.assertEqual(
            self._creates_of(cs, "tenancy.tenant"), [],
            "the auto-slug matcher should have bound the grouped tenant",
        )
        self.assertEqual(self._creates_of(cs, "ipam.vrf"), [])

        self._apply(cs)
        self.assertEqual(Tenant.objects.filter(name="Corp").count(), 1)
        self.assertEqual(VRF.objects.filter(name="CORP-CORE").count(), 1)
        self.assertEqual(
            Prefix.objects.get(prefix="10.1.0.0/24").tenant_id, self.corp.pk
        )


class AutoSlugRescueFailsTestCase(_IngestMixin, TestCase):
    """2. Where the rescue fails, the cascade fires and duplicates a VRF."""

    @classmethod
    def setUpTestData(cls):
        """Same tenant, but its stored slug is NOT slugify(name).

        Entirely legal in NetBox, and common wherever an operator renamed a
        tenant or seeded slugs by a local convention.
        """
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(
            name="Corp", slug="corp-emea", group=cls.group
        )
        cls.corp_core = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)

    def test_groupless_tenant_cascades_into_a_duplicate_vrf(self):
        """THE CASCADE: a correct VRF reference duplicates because its tenant did.

        The vrf reference is exactly right for CORP-CORE -- name plus tenant,
        no RD. The only defect is one level down: the nested tenant omits its
        group, and this time no slug coincidence rescues it.
        """
        entity = self._prefix_entity(
            "10.1.0.0/24",
            vrf={"name": "CORP-CORE", "tenant": {"name": "Corp"}},
            tenant={"name": "Corp"},
        )
        cs = self._plan(entity)

        self.assertNotEqual(
            self._creates_of(cs, "tenancy.tenant"), [],
            "expected the group-less reference to miss the grouped tenant",
        )
        self.assertNotEqual(
            self._creates_of(cs, "ipam.vrf"), [],
            "CASCADE BROKEN: the VRF matched despite its tenant being unresolved",
        )

        self._apply(cs)
        self.assertEqual(VRF.objects.filter(name="CORP-CORE").count(), 2)
        self.assertEqual(Tenant.objects.filter(name="Corp").count(), 2)

    def test_carrying_the_group_prevents_the_cascade(self):
        """THE FIX: with the group, the tenant resolves and the VRF matches.

        Note this works even though the slug still does not match -- the
        (group, name) constraint is what binds it.
        """
        entity = self._prefix_entity(
            "10.1.0.0/24",
            vrf={"name": "CORP-CORE",
                 "tenant": {"name": "Corp", "group": {"name": "Labs"}}},
            tenant={"name": "Corp", "group": {"name": "Labs"}},
        )
        cs = self._plan(entity)

        self.assertEqual(self._creates_of(cs, "tenancy.tenant"), [])
        self.assertEqual(self._creates_of(cs, "ipam.vrf"), [])

        self._apply(cs)
        self.assertEqual(VRF.objects.filter(name="CORP-CORE").count(), 1)
        self.assertEqual(Tenant.objects.filter(name="Corp").count(), 1)


class AutoSlugMisbindTestCase(_IngestMixin, TestCase):
    """3. The rescue matches by slug alone, so the group never decides."""

    @classmethod
    def setUpTestData(cls):
        """Two tenants sharing a slug in different groups -- legal in NetBox.

        NetBox constrains (group, slug), not slug, so this is a valid estate.
        """
        cls.labs = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.prod = TenantGroup.objects.create(name="Prod", slug="prod")
        cls.corp_labs = Tenant.objects.create(
            name="Corp", slug="corp", group=cls.labs
        )
        cls.corp_prod = Tenant.objects.create(
            name="Corp", slug="corp", group=cls.prod
        )
        cls.lab_a = VRF.objects.create(name="LAB-A")

    def test_groupless_reference_binds_the_oldest_slug_match(self):
        """A group-less reference cannot express WHICH Corp it means.

        It binds whichever row is oldest, silently. An operator intending the
        Prod tenant gets the Labs one, with no error anywhere.
        """
        entity = self._prefix_entity(
            "10.2.0.0/24",
            vrf={"name": "LAB-A"},
            tenant={"name": "Corp"},
        )
        self._ingest(entity)

        self.assertEqual(Tenant.objects.filter(name="Corp").count(), 2)
        self.assertEqual(
            Prefix.objects.get(prefix="10.2.0.0/24").tenant_id,
            self.corp_labs.pk,
            "bound by slug alone -- the group played no part",
        )

    def test_group_disambiguates_it(self):
        """Carrying the group is the only way to name the tenant you mean."""
        entity = self._prefix_entity(
            "10.3.0.0/24",
            vrf={"name": "LAB-A"},
            tenant={"name": "Corp", "group": {"name": "Prod"}},
        )
        self._ingest(entity)

        self.assertEqual(
            Prefix.objects.get(prefix="10.3.0.0/24").tenant_id, self.corp_prod.pk
        )
        self.assertEqual(Tenant.objects.filter(name="Corp").count(), 2)


class CorrectedPlacementTestCase(_IngestMixin, TestCase):
    """The agent's current payload, end to end, against the prebuilt estate."""

    @classmethod
    def setUpTestData(cls):
        """The prebuilt estate the policy targets."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)
        cls.lab_a_tenant = Tenant.objects.create(
            name="Lab-A", slug="lab-a", group=cls.group
        )
        cls.corp_core = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)
        cls.lab_a = VRF.objects.create(name="LAB-A")

    def test_corrected_reference_converges_on_re_ingest(self):
        """A second identical scan must plan nothing at all."""
        entity = self._prefix_entity(
            "10.1.0.0/24",
            vrf={"name": "CORP-CORE",
                 "tenant": {"name": "Corp", "group": {"name": "Labs"}}},
            tenant={"name": "Corp", "group": {"name": "Labs"}},
        )
        self._ingest(entity)
        cs = self._plan(entity)

        non_noop = [c for c in cs.changes if c.change_type != ChangeType.NOOP]
        self.assertEqual(non_noop, [], [c.to_dict() for c in non_noop])
        self.assertEqual(VRF.objects.filter(name="CORP-CORE").count(), 1)

    def test_name_only_reference_binds_the_bare_vrf(self):
        """LAB-A has no tenant and no RD, so a name-only reference is correct."""
        entity = self._prefix_entity(
            "10.2.0.0/24",
            vrf={"name": "LAB-A"},
            tenant={"name": "Lab-A", "group": {"name": "Labs"}},
        )
        cs = self._plan(entity)

        self.assertEqual(self._creates_of(cs, "ipam.vrf"), [])
        self._apply(cs)
        self.assertEqual(VRF.objects.filter(name="LAB-A").count(), 1)
        self.assertEqual(
            Prefix.objects.get(prefix="10.2.0.0/24").vrf_id, self.lab_a.pk
        )

    def test_two_subnets_land_in_their_own_vrfs(self):
        """One policy, two prebuilt VRFs -- the capability per-subnet placement added."""
        self._ingest(self._prefix_entity(
            "10.1.0.0/24",
            vrf={"name": "CORP-CORE",
                 "tenant": {"name": "Corp", "group": {"name": "Labs"}}},
            tenant={"name": "Corp", "group": {"name": "Labs"}},
        ))
        self._ingest(self._prefix_entity(
            "10.2.0.0/24",
            vrf={"name": "LAB-A"},
            tenant={"name": "Lab-A", "group": {"name": "Labs"}},
        ))

        self.assertEqual(VRF.objects.count(), 2)
        self.assertEqual(
            Prefix.objects.get(prefix="10.1.0.0/24").vrf_id, self.corp_core.pk
        )
        self.assertEqual(
            Prefix.objects.get(prefix="10.2.0.0/24").vrf_id, self.lab_a.pk
        )

    def test_address_carries_the_same_vrf_object_as_its_prefix(self):
        """An address and its prefix carrying the same VRF object, end to end."""
        vrf_ref = {"name": "CORP-CORE",
                   "tenant": {"name": "Corp", "group": {"name": "Labs"}}}
        tenant_ref = {"name": "Corp", "group": {"name": "Labs"}}

        self._ingest(self._prefix_entity("10.1.0.0/24", vrf_ref, tenant_ref))
        self._ingest(
            {"address": "10.1.0.5/24", "vrf": vrf_ref, "tenant": tenant_ref},
            "ipam.ipaddress",
        )

        addr = IPAddress.objects.get(address="10.1.0.5/24")
        self.assertEqual(addr.vrf_id, self.corp_core.pk)
        self.assertEqual(VRF.objects.filter(name="CORP-CORE").count(), 1)
