"""The criteria that decide whether an ipam.vrf reference matches or creates.

ipam.vrf has no DB unique constraint on name, so matching rests on the two
logical criteria in _LOGICAL_MATCHERS plus the unique_rd field matcher. The
consequences are easy to get wrong when writing a producer, and invisible when
you do: an unmatched reference is not an error, it is a second VRF.

  name only        can only find a VRF with neither rd nor tenant
  name + tenant    can only find a VRF with that tenant and no rd
  rd               matches on rd ALONE; the payload's name is not a filter

These tests pin that behaviour so a change to the criteria has to be deliberate.
"""
from ipam.models import VRF
from django.test import TestCase
from tenancy.models import Tenant, TenantGroup

from netbox_diode_plugin.api.common import UnresolvedReference
from netbox_diode_plugin.api.matcher import find_existing_object, get_model_matchers


class VRFMatcherContractTests(TestCase):
    """A. The three VRF criteria, exactly as the generated docs describe them."""

    @classmethod
    def setUpTestData(cls):
        """Two prebuilt VRFs of the shapes the policy targets, plus an RD one."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)
        # CORP-CORE: has a tenant, no RD
        cls.corp_core = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)
        # LAB-A: neither tenant nor RD
        cls.lab_a = VRF.objects.create(name="LAB-A")
        # a VRF distinguished only by RD
        cls.rd_vrf = VRF.objects.create(name="RD-VRF", rd="65000:1")

    def test_name_only_matches_bare_vrf(self):
        """Table row 1: name-only finds a VRF with neither RD nor tenant."""
        found = find_existing_object({"name": "LAB-A"}, "ipam.vrf")
        self.assertEqual(found, self.lab_a)

    def test_name_only_does_not_match_tenanted_vrf(self):
        """Table row 1, the consequence: name-only CANNOT find a tenanted VRF.

        This is the duplicate. Diode answers None by creating a second VRF.
        """
        found = find_existing_object({"name": "CORP-CORE"}, "ipam.vrf")
        self.assertIsNone(found)

    def test_name_and_tenant_matches_tenanted_vrf(self):
        """Table row 2: name+tenant finds the tenanted VRF."""
        found = find_existing_object(
            {"name": "CORP-CORE", "tenant": self.corp.pk}, "ipam.vrf"
        )
        self.assertEqual(found, self.corp_core)

    def test_rd_matches_regardless_of_name(self):
        """Table row 3, sharpened: unique_rd matches on RD alone.

        The payload's name is not a filter, so a wrong name paired with a right
        RD still matches -- and the differ would then rename the VRF.
        """
        found = find_existing_object(
            {"name": "TOTALLY-DIFFERENT-NAME", "rd": "65000:1"}, "ipam.vrf"
        )
        self.assertEqual(found, self.rd_vrf)

    def test_criteria_order_is_logical_then_builtin(self):
        """find_existing_object walks criteria in this published order."""
        names = [m.name for m in get_model_matchers(VRF)]
        self.assertEqual(
            names[:3],
            [
                "logical_vrf_name_no_tenant",
                "logical_vrf_name_within_tenant",
                "unique_rd",
            ],
        )


class ExistingDuplicateTests(TestCase):
    """B. Does a bare duplicate keep stealing writes from a corrected reference?"""

    @classmethod
    def setUpTestData(cls):
        """The prebuilt tenanted VRF, plus the bare duplicate an earlier run made."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)
        cls.prebuilt = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)
        # created later by a name-only reference that failed to match
        cls.duplicate = VRF.objects.create(name="CORP-CORE")

    def test_corrected_reference_finds_prebuilt_not_duplicate(self):
        """A common assumption is the duplicate may keep winning. It does not.

        Criterion 1 is gated off at the payload (it carries a tenant), and
        criterion 2 filters tenant=Corp, which the bare duplicate fails.
        """
        found = find_existing_object(
            {"name": "CORP-CORE", "tenant": self.corp.pk}, "ipam.vrf"
        )
        self.assertEqual(found, self.prebuilt)
        self.assertNotEqual(found, self.duplicate)

    def test_name_only_reference_still_finds_the_duplicate(self):
        """An UNCORRECTED reference keeps feeding the duplicate, as expected."""
        found = find_existing_object({"name": "CORP-CORE"}, "ipam.vrf")
        self.assertEqual(found, self.duplicate)

    def test_two_bare_duplicates_resolve_to_the_oldest(self):
        """Same-criterion duplicates are broken by order_by('pk').first()."""
        first = VRF.objects.create(name="LAB-A")
        VRF.objects.create(name="LAB-A")
        found = find_existing_object({"name": "LAB-A"}, "ipam.vrf")
        self.assertEqual(found, first)


class TenantGroupTrapTests(TestCase):
    """C. The tenant-group trap, and which constraint enforces it on 4.6."""

    @classmethod
    def setUpTestData(cls):
        """A grouped tenant -- the shape a group-less reference cannot find."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)

    def test_groupless_reference_does_not_find_grouped_tenant(self):
        """The trap, for the name/group matchers ALONE.

        Read with care: this calls the matcher directly, so the payload
        carries no `_auto_slug`. The real plan path always supplies one, and
        AutoSlugMatcher then matches on slug alone with no group condition --
        which usually rescues a group-less reference. See
        test_vrf_tenant_cascade_ingest.AutoSlugRescueTestCase for what the
        end-to-end path actually does, and AutoSlugRescueFailsTestCase for
        the case where this trap really does bite.
        """
        found = find_existing_object({"name": "Corp"}, "tenancy.tenant")
        self.assertIsNone(found)

    def test_reference_with_group_finds_it(self):
        """Carrying the group is what makes the reference match."""
        found = find_existing_object(
            {"name": "Corp", "group": self.group.pk}, "tenancy.tenant"
        )
        self.assertEqual(found, self.corp)

    def test_which_constraint_shape_this_netbox_has(self):
        """Pin the 4.6 mechanism: a partial constraint, not nulls_distinct=False.

        That comment describes the 4.7 mechanism. If this assertion ever fails,
        NetBox has collapsed the partial constraint and the producer's stated
        mechanism has become the right one.
        """
        by_name = {m.name: m for m in get_model_matchers(Tenant)}
        self.assertIn("tenancy_tenant_unique_group_name", by_name)
        self.assertIn("tenancy_tenant_unique_name", by_name)
        group_name = by_name["tenancy_tenant_unique_group_name"]
        # 4.6: a plain UniqueConstraint, so absence of `group` DISQUALIFIES it
        self.assertFalse(group_name.nulls_not_distinct)
        self.assertFalse(group_name.has_required_fields({"name": "Corp"}))


class UnresolvedTenantTests(TestCase):
    """D. Does an unresolved nested tenant make the VRF reference fall through?"""

    @classmethod
    def setUpTestData(cls):
        """The prebuilt tenanted VRF the reference is trying to find."""
        cls.group = TenantGroup.objects.create(name="Labs", slug="labs")
        cls.corp = Tenant.objects.create(name="Corp", slug="corp", group=cls.group)
        cls.corp_core = VRF.objects.create(name="CORP-CORE", tenant=cls.corp)

    def test_unresolved_tenant_makes_the_vrf_unmatchable(self):
        """A correctly-shaped reference finds NOTHING while its tenant is unresolved.

        Criterion 1 is gated off (the payload carries a tenant), criterion 2
        cannot build a queryset from an UnresolvedReference, and unique_rd has
        no rd. Every criterion declines, so the caller creates a second VRF --
        a duplicate produced by a payload that is entirely correct.
        """
        pending_tenant = UnresolvedReference(
            object_type="tenancy.tenant", uuid="00000000-0000-0000-0000-000000000001"
        )
        found = find_existing_object(
            {"name": "CORP-CORE", "tenant": pending_tenant}, "ipam.vrf"
        )
        self.assertIsNone(found)

    def test_same_reference_matches_once_the_tenant_is_resolved(self):
        """The control: identical payload, tenant resolved to a pk, matches."""
        found = find_existing_object(
            {"name": "CORP-CORE", "tenant": self.corp.pk}, "ipam.vrf"
        )
        self.assertEqual(found, self.corp_core)
