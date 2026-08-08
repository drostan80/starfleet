"""schema.graphql validity — BUILD_PLAN.md A.2.

No resolvers exist yet (A.3), so this only checks the SDL itself is
well-formed and loads the way the app will actually load it at
runtime (importlib.resources, package-relative — not a hardcoded path,
so this also guards the pyproject.toml package-data fix that makes a
real `pip install .` ship schema.graphql at all).
"""

from importlib import resources

from ariadne import make_executable_schema
from graphql import validate_schema


def _load_sdl() -> str:
    return resources.files("lcars").joinpath("schema.graphql").read_text()


def test_schema_is_valid():
    schema = make_executable_schema(_load_sdl())
    assert validate_schema(schema) == []


def test_query_and_mutation_have_the_eight_scope_md_section_8_query_shapes():
    schema = make_executable_schema(_load_sdl())
    query_fields = schema.query_type.fields
    # SCOPE.md §8's six deliberately-designed query shapes.
    for field in (
        "showsByStatus",
        "episodesAiringSoon",
        "pendingReviews",
        "backlog",
        "search",
        "nextUp",
    ):
        assert field in query_fields, f"missing §8 query shape: {field}"


def test_mutations_are_dedicated_field_specific_not_generic_update():
    schema = make_executable_schema(_load_sdl())
    mutation_fields = schema.mutation_type.fields
    assert "updateShow" not in mutation_fields  # §3 principle 7
    for field in (
        "setStatus",
        "setScore",
        "addWatchEvent",
        "markEpisodeSkipped",
        "markSeasonWatched",
    ):
        assert field in mutation_fields
