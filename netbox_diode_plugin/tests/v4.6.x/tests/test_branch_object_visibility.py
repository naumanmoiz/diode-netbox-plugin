"""What an active NetBox Branching branch does to object matching.

Diode plans and applies inside a branch's schema whenever one is active, and
_get_branch_schema_id falls back to the branch stored in the plugin's own Setting
when no X-NetBox-Branch header is sent -- so a branch can be in force with
nothing in the producer's configuration naming it.

Branch provisioning snapshots main at that moment. An object created in main
afterwards does not exist in the branch, cannot be matched there, and is created
again inside it -- so the same object exists twice the moment the branch merges,
while anything reading main over the REST API reports it as present and fine.

The failure is therefore about ORDERING, not about branching as such, and these
two tests are the control pair that shows it: same branch, same object, differing
only in whether the object predates provisioning.

Rule for operators: prebuild objects BEFORE provisioning a branch, and sync the
branch after any later change to them.
"""
from ipam.models import VRF, Prefix
from django.test import TransactionTestCase
from tenancy.models import Tenant, TenantGroup
from users.models import User

from netbox_diode_plugin.api.applier import apply_changeset
from netbox_diode_plugin.api.common import ChangeType
from netbox_diode_plugin.api.differ import generate_changeset


class PrefixBranchTestCase(TransactionTestCase):
    """Branch provisioning needs real transactions, so not TestCase."""

    def setUp(self):
        self.user = User.objects.create(username="visibility-test", is_superuser=True)
        self.group = TenantGroup.objects.create(
            name="Example Labs", slug="example-labs"
        )
        self.tenant = Tenant.objects.create(
            name="Tenant A", slug="tenant-a", group=self.group
        )
        self.vrf = VRF.objects.create(name="VRF-A", tenant=self.tenant)

    def _entity(self):
        return {
            "prefix": "10.0.0.0/16",
            "vrf": {"name": "VRF-A",
                    "tenant": {"name": "Tenant A",
                               "group": {"name": "Example Labs"}}},
            "tenant": {"name": "Tenant A", "group": {"name": "Example Labs"}},
        }

    def _plan(self):
        return generate_changeset(self._entity(), "ipam.prefix").change_set

    def _creates(self, cs):
        return [c for c in cs.changes
                if c.object_type == "ipam.prefix"
                and c.change_type == ChangeType.CREATE]

    def test_prefix_made_in_main_after_the_branch_exists(self):
        """The suspected production shape: branch first, operator prefix second."""
        from netbox_branching.models import Branch
        from netbox_branching.utilities import activate_branch

        branch = Branch(name="ingest-branch")
        branch.save(provision=False)
        branch.provision(self.user)
        branch.refresh_from_db()

        # operator creates the prefix in MAIN, after the branch was provisioned
        Prefix.objects.create(prefix="10.0.0.0/16", vrf=self.vrf, tenant=self.tenant)
        self.assertEqual(Prefix.objects.filter(prefix="10.0.0.0/16").count(), 1)

        with activate_branch(branch):
            cs = self._plan()
            creates = self._creates(cs)
            in_branch_before = Prefix.objects.filter(prefix="10.0.0.0/16").count()
            if cs.changes:
                apply_changeset(cs, request=None)
            in_branch_after = Prefix.objects.filter(prefix="10.0.0.0/16").count()

        in_main = Prefix.objects.filter(prefix="10.0.0.0/16").count()
        print(f"\n[branch] rows visible in branch before plan: {in_branch_before}")
        print(f"[branch] prefix CREATEs planned: {len(creates)}")
        print(f"[branch] rows in branch after apply: {in_branch_after}")
        print(f"[branch] rows in main after apply:   {in_main}")

        # Pin whatever actually happens; the printout above is the finding.
        self.assertEqual(len(creates), 1 if in_branch_before == 0 else 0)

    def test_prefix_made_in_main_before_the_branch_exists(self):
        """The control: same branch, but the prefix predates it.

        Provisioning copies main into the branch schema, so a row that already
        existed IS visible there and matches. This is what makes the failure
        depend on ORDERING rather than on branching as such.
        """
        from netbox_branching.models import Branch
        from netbox_branching.utilities import activate_branch

        # operator creates the prefix in MAIN first
        Prefix.objects.create(prefix="10.0.0.0/16", vrf=self.vrf, tenant=self.tenant)

        branch = Branch(name="ingest-branch-2")
        branch.save(provision=False)
        branch.provision(self.user)
        branch.refresh_from_db()

        with activate_branch(branch):
            visible = Prefix.objects.filter(prefix="10.0.0.0/16").count()
            cs = self._plan()
            creates = self._creates(cs)
            if cs.changes:
                apply_changeset(cs, request=None)
            after = Prefix.objects.filter(prefix="10.0.0.0/16").count()

        print(f"\n[control] rows visible in branch before plan: {visible}")
        print(f"[control] prefix CREATEs planned: {len(creates)}")
        print(f"[control] rows in branch after apply: {after}")
        self.assertEqual(visible, 1)
        self.assertEqual(creates, [])
        self.assertEqual(after, 1)
