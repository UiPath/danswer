"""Guards for the persona-serialization eager-load (N+1 fix).

PersonaSnapshot.from_model walks document_sets -> connector_credential_pairs ->
connector/credential, all lazy by default. get_personas / get_persona_by_id take
an opt-in `eager_load` flag that selectin-loads that whole chain so serializing
the admin assistants list / edit page doesn't fire hundreds of queries.

These tests are DB-free. They guard the two ways this fix silently breaks:
  1. A relationship gets renamed -> the eager-load chain references a dead
     attribute (would raise, or quietly revert to N+1). Asserting the exact
     relationship names exist (and that building the options doesn't raise)
     catches that.
  2. `eager_load` stops defaulting to False -> the visibility toggle / delete
     paths (which call get_persona_by_id) start paying for the heavy load they
     don't need. Asserting the default stays False catches that.
"""
import inspect

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from danswer.db.models import ConnectorCredentialPair
from danswer.db.models import DocumentSet
from danswer.db.models import Persona
from danswer.db.persona import _persona_snapshot_load_options
from danswer.db.persona import get_persona_by_id
from danswer.db.persona import get_personas


def test_load_options_builds_without_error() -> None:
    # Constructing the options dereferences every relationship in the chain
    # (Persona.document_sets, DocumentSet.connector_credential_pairs, ...), so a
    # rename would raise here.
    opts = _persona_snapshot_load_options()
    assert len(opts) == 8


def test_options_compose_onto_a_persona_select() -> None:
    stmt = select(Persona).options(*_persona_snapshot_load_options())
    assert stmt is not None


def test_eager_chain_relationships_exist() -> None:
    persona_rels = sa_inspect(Persona).relationships
    for rel in ["user", "prompts", "tools", "users", "groups", "document_sets"]:
        assert rel in persona_rels, f"Persona.{rel} relationship missing"

    docset_rels = sa_inspect(DocumentSet).relationships
    for rel in ["connector_credential_pairs", "users", "groups"]:
        assert rel in docset_rels, f"DocumentSet.{rel} relationship missing"

    ccpair_rels = sa_inspect(ConnectorCredentialPair).relationships
    for rel in ["connector", "credential"]:
        assert rel in ccpair_rels, f"ConnectorCredentialPair.{rel} relationship missing"


def test_eager_load_defaults_to_false() -> None:
    # Must stay False so the visibility toggle / delete paths don't pay for the
    # heavy serialization load they never use.
    assert get_personas.__defaults__ is not None
    assert inspect.signature(get_personas).parameters["eager_load"].default is False
    assert (
        inspect.signature(get_persona_by_id).parameters["eager_load"].default is False
    )
