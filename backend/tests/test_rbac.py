"""
Business-rule tests for entity-scoped authorization (SECTION 5/14):
a user with a role granting a permission should only be authorized
for entities within their assigned scope.
"""
import uuid

from app.auth.dependencies import _grants_permission
from app.models.rbac import (
    EntityScopeType,
    Permission,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)


def _make_user_with_assignment(scope_type: EntityScopeType, legal_entity_id=None) -> User:
    role = Role(id=uuid.uuid4(), name="Test Role")
    role.permissions = [
        Permission(
            id=uuid.uuid4(),
            role_id=role.id,
            module=TreasuryModule.BANK_RECONCILIATION,
            action=TreasuryAction.VIEW,
        )
    ]
    assignment = UserRoleAssignment(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role_id=role.id,
        scope_type=scope_type,
        legal_entity_id=legal_entity_id,
        is_active=True,
    )
    assignment.role = role
    user = User(id=uuid.uuid4(), email="x@x.com", full_name="X", hashed_password="x")
    user.role_assignments = [assignment]
    return user


def test_group_wide_grant_allows_any_entity():
    user = _make_user_with_assignment(EntityScopeType.GROUP_WIDE)
    some_entity = uuid.uuid4()
    assert _grants_permission(
        user, TreasuryModule.BANK_RECONCILIATION, TreasuryAction.VIEW, some_entity
    )


def test_entity_scoped_grant_allows_matching_entity_only():
    entity_a = uuid.uuid4()
    entity_b = uuid.uuid4()
    user = _make_user_with_assignment(EntityScopeType.ENTITY, legal_entity_id=entity_a)

    assert _grants_permission(
        user, TreasuryModule.BANK_RECONCILIATION, TreasuryAction.VIEW, entity_a
    )
    assert not _grants_permission(
        user, TreasuryModule.BANK_RECONCILIATION, TreasuryAction.VIEW, entity_b
    )


def test_missing_permission_denied():
    user = _make_user_with_assignment(EntityScopeType.GROUP_WIDE)
    assert not _grants_permission(
        user, TreasuryModule.PAYMENTS, TreasuryAction.APPROVE, None
    )


def test_superuser_bypasses_scope():
    user = _make_user_with_assignment(EntityScopeType.ENTITY, legal_entity_id=uuid.uuid4())
    user.is_superuser = True
    assert _grants_permission(
        user, TreasuryModule.PAYMENTS, TreasuryAction.APPROVE, uuid.uuid4()
    )
