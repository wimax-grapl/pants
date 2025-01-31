# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from textwrap import dedent
from typing import Set

import pytest

from pants.backend.codegen.thrift import dependency_inference
from pants.backend.codegen.thrift.dependency_inference import (
    InferThriftDependencies,
    ThriftMapping,
    parse_thrift_imports,
)
from pants.backend.codegen.thrift.target_types import (
    ThriftSourceField,
    ThriftSourcesGeneratorTarget,
)
from pants.backend.codegen.thrift.target_types import rules as target_types_rules
from pants.core.util_rules import stripped_source_files
from pants.engine.addresses import Address
from pants.engine.target import InferredDependencies
from pants.testutil.rule_runner import QueryRule, RuleRunner
from pants.util.frozendict import FrozenDict


@pytest.mark.parametrize(
    "file_content,expected",
    [
        ("include 'foo.thrift'", {"foo.thrift"}),
        ("include'foo.thrift'", {"foo.thrift"}),
        ("include\t   'foo.thrift'   \t", {"foo.thrift"}),
        ('include "foo.thrift"', {"foo.thrift"}),
        # We don't worry about matching opening and closing ' vs. " quotes; Thrift will error if
        # invalid.
        ("include 'foo.thrift\"", {"foo.thrift"}),
        ("include \"foo.thrift'", {"foo.thrift"}),
        # More complex file names.
        ("include 'path/to_dir/f.thrift'", {"path/to_dir/f.thrift"}),
        ("include 'path\\to-dir\\f.thrift'", {"path\\to-dir\\f.thrift"}),
        ("include 'âčĘï.thrift'", {"âčĘï.thrift"}),
        ("include '123.thrift'", {"123.thrift"}),
        # Invalid includes.
        ("include foo.thrift", set()),
        ("inlude 'foo.thrift'", set()),
        # Multiple includes in a file.
        ("include 'foo.thrift'; include \"bar.thrift\";", {"foo.thrift", "bar.thrift"}),
        (
            dedent(
                """\
                include 'dir/foo.thrift'
                some random thrift code;
                include 'ábč.thrift';
                """
            ),
            {"dir/foo.thrift", "ábč.thrift"},
        ),
    ],
)
def test_parse_thrift_imports(file_content: str, expected: Set[str]) -> None:
    assert set(parse_thrift_imports(file_content)) == expected


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *stripped_source_files.rules(),
            *dependency_inference.rules(),
            *target_types_rules(),
            QueryRule(ThriftMapping, []),
            QueryRule(InferredDependencies, [InferThriftDependencies]),
        ],
        target_types=[ThriftSourcesGeneratorTarget],
    )


def test_thrift_mapping(rule_runner: RuleRunner) -> None:
    rule_runner.set_options(["--source-root-patterns=['root1', 'root2', 'root3']"])
    rule_runner.write_files(
        {
            # Two proto files belonging to the same target. We should use two file addresses.
            "root1/thrifts/f1.thrift": "",
            "root1/thrifts/f2.thrift": "",
            "root1/thrifts/BUILD": "thrift_sources()",
            # These thrifts would result in the same stripped file name, so they are ambiguous.
            "root1/two_owners/f.thrift": "",
            "root1/two_owners/BUILD": "thrift_sources()",
            "root2/two_owners/f.thrift": "",
            "root2/two_owners/BUILD": "thrift_sources()",
        }
    )
    result = rule_runner.request(ThriftMapping, [])
    assert result == ThriftMapping(
        mapping=FrozenDict(
            {
                "thrifts/f1.thrift": Address("root1/thrifts", relative_file_path="f1.thrift"),
                "thrifts/f2.thrift": Address("root1/thrifts", relative_file_path="f2.thrift"),
            }
        ),
        ambiguous_modules=FrozenDict(
            {
                "two_owners/f.thrift": (
                    Address("root1/two_owners", relative_file_path="f.thrift"),
                    Address("root2/two_owners", relative_file_path="f.thrift"),
                )
            }
        ),
    )


def test_dependency_inference(rule_runner: RuleRunner, caplog) -> None:
    rule_runner.set_options(["--source-root-patterns=['src/thrifts']"])
    rule_runner.write_files(
        {
            "src/thrifts/project/f1.thrift": dedent(
                """\
                include 'tests/f.thrift';
                include 'unrelated_path/foo.thrift";
                """
            ),
            "src/thrifts/project/f2.thrift": "include 'project/f1.thrift';",
            "src/thrifts/project/BUILD": "thrift_sources()",
            "src/thrifts/tests/f.thrift": "",
            "src/thrifts/tests/BUILD": "thrift_sources()",
            # Test handling of ambiguous imports. We should warn on the ambiguous dependency, but
            # not warn on the disambiguated one and should infer a dep.
            "src/thrifts/ambiguous/dep.thrift": "",
            "src/thrifts/ambiguous/disambiguated.thrift": "",
            "src/thrifts/ambiguous/main.thrift": dedent(
                """\
                include 'ambiguous/dep.thrift';
                include 'ambiguous/disambiguated.thrift";
                """
            ),
            "src/thrifts/ambiguous/BUILD": dedent(
                """\
                thrift_sources(name='dep1', sources=['dep.thrift', 'disambiguated.thrift'])
                thrift_sources(name='dep2', sources=['dep.thrift', 'disambiguated.thrift'])
                thrift_sources(
                    name='main',
                    sources=['main.thrift'],
                    dependencies=['!./disambiguated.thrift:dep2'],
                )
                """
            ),
        }
    )

    def run_dep_inference(address: Address) -> InferredDependencies:
        tgt = rule_runner.get_target(address)
        return rule_runner.request(
            InferredDependencies, [InferThriftDependencies(tgt[ThriftSourceField])]
        )

    assert run_dep_inference(
        Address("src/thrifts/project", relative_file_path="f1.thrift")
    ) == InferredDependencies([Address("src/thrifts/tests", relative_file_path="f.thrift")])
    assert run_dep_inference(
        Address("src/thrifts/project", relative_file_path="f2.thrift")
    ) == InferredDependencies([Address("src/thrifts/project", relative_file_path="f1.thrift")])

    caplog.clear()
    assert run_dep_inference(
        Address("src/thrifts/ambiguous", target_name="main", relative_file_path="main.thrift")
    ) == InferredDependencies(
        [
            Address(
                "src/thrifts/ambiguous",
                target_name="dep1",
                relative_file_path="disambiguated.thrift",
            )
        ]
    )
    assert len(caplog.records) == 1
    assert (
        "The target src/thrifts/ambiguous/main.thrift:main imports `ambiguous/dep.thrift`"
        in caplog.text
    )
    assert (
        "['src/thrifts/ambiguous/dep.thrift:dep1', 'src/thrifts/ambiguous/dep.thrift:dep2']"
        in caplog.text
    )
    assert "disambiguated.thrift" not in caplog.text
