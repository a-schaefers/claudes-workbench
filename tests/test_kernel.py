"""The PureScript kernel's boundary, held offline (kernel/BOUNDARY.md, #1356): no toolchain needed,
so `python3 -m unittest` runs it for a TypeScript, Python or docs change. CI's kernel job runs
`scripts/kernel.py check`, which rebuilds the bundle with the pinned toolchain on top of this.

Each failure names its fix. The Checker class breaks a copy of the tree in each way a check must
catch (never the tree itself) and asserts that the break fails by name."""
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import kernel  # noqa: E402
from workbench import store  # noqa: E402

FIX = "run python3 scripts/kernel.py build"


def tar_of(members) -> bytes:
    """A gzipped tar from (name, kind, data): kind 'f' a file, 'd' a directory, 'l' a symlink,
    'h' a hard link, 'c' a character device."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            if kind == "f":
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
                continue
            info.type = {"d": tarfile.DIRTYPE, "l": tarfile.SYMTYPE, "h": tarfile.LNKTYPE, "c": tarfile.CHRTYPE}[kind]
            info.linkname = data.decode() if kind in "lh" else ""
            tf.addfile(info)
    return buf.getvalue()


class Contract(unittest.TestCase):
    """BOUNDARY.md, the bundle, its d.ts and the goldens agree, and the membrane holds."""

    def test_the_committed_kernel_passes_every_offline_check(self):
        self.assertEqual(kernel.offline_errors(), [])

    def test_boundary_the_bundle_the_dts_and_the_goldens_name_the_same_exports(self):
        p = kernel.paths()
        names = sorted(kernel.boundary_exports())
        self.assertEqual(sorted(kernel.bundle_exports(p["bundle"].read_bytes())), names, "a new export gets its row")
        self.assertEqual(sorted(kernel.dts_exports(p["dts"].read_text())), names, FIX)
        self.assertEqual(kernel.golden_fns(), names, "each export gets golden cases in mod/hooks/golden/")

    def test_only_core_ts_imports_the_kernel(self):
        self.assertEqual(kernel.kernel_importers(), ["mod/hooks/core.ts"])

    def test_the_denied_imports_are_the_ones_boundary_md_lists(self):
        self.assertEqual(kernel.boundary_denied(), list(kernel.DENIED), "scripts/kernel.py's DENIED and BOUNDARY.md move together")

    def test_the_budgets_hold(self):
        self.assertEqual(kernel.budget_errors(), [])
        self.assertEqual(set(kernel.boundary_budget()),
                         {"exports", "kernel.js bytes", "kernel/src lines", "module lines", "core.ts lines"})

    def test_no_name_an_export_replaces_is_still_defined(self):
        self.assertEqual(kernel.replaced_still_defined(), [])

    def test_the_plugin_spells_no_phase_of_its_own(self):
        self.assertEqual(kernel.phase_literals(), [])


class Bundle(unittest.TestCase):
    """The committed kernel.js and kernel.d.ts are what their banners say they are built from."""

    def test_each_banner_names_its_body_the_inputs_and_the_pinned_tools(self):
        pin, inputs = kernel.load_pin(), kernel.inputs_digest()
        for target in (kernel.paths()["bundle"], kernel.paths()["dts"]):
            with self.subTest(file=target.name):
                data = target.read_bytes()
                got = kernel.read_banner(data)
                self.assertIsNotNone(got, FIX)
                was, body, purs, esbuild = got
                self.assertEqual(body, hashlib.sha256(data.partition(b"\n")[2]).hexdigest(), f"edited by hand: {FIX}")
                self.assertEqual(was, inputs, f"stale: {FIX}")
                self.assertEqual((purs, esbuild), (pin["purs"]["version"], pin["esbuild"]["version"]))

    def test_the_bundle_holds_no_effect_host_clock_chance_or_locale(self):
        js = kernel.paths()["bundle"].read_text()
        self.assertEqual([t for t in kernel.BUNDLE_DENIED if t in js], [])

    def test_the_bundle_is_one_es_module_that_imports_nothing(self):
        js = kernel.paths()["bundle"].read_text()
        self.assertNotRegex(js, r"(?m)^\s*import\b")
        self.assertRegex(js, r"export\s*\{[^}]*\};?\s*\Z")

    def test_bundle_exports_reads_esbuilds_closing_export(self):
        self.assertEqual(kernel.bundle_exports("var a;\nexport {\n  phaseOf,\n  x as tables\n};\n"), ["phaseOf", "tables"])
        self.assertEqual(kernel.bundle_exports("var a;\n"), [])


class Tables(unittest.TestCase):
    def test_tables_purs_is_what_workbenchs_python_says(self):
        self.assertTrue(kernel.tables(check=True), FIX)

    def test_tables_copies_the_phase_sets_sorted_and_the_two_constants(self):
        v = kernel.tables_values()
        self.assertEqual(v["active"], sorted(store.ACTIVE))
        self.assertEqual(v["needsClaude"], sorted(store.NEEDS_CLAUDE))
        self.assertEqual(v["parks"], sorted(store.PARKS))
        self.assertEqual(v["closed"], sorted(store.CLOSED))
        self.assertEqual(v["noAnswer"], "No answer arrived in time.")
        self.assertIsInstance(v["chatStaleMs"], float)

    def test_a_string_with_a_quote_a_backslash_or_non_ascii_is_a_purescript_literal(self):
        self.assertEqual(kernel.purs_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(kernel.purs_string("é"), '"\\x0000e9"')


class Imports(unittest.TestCase):
    def test_kernel_src_has_no_ffi_and_no_denied_import(self):
        self.assertEqual(kernel.lint_sources(), [])

    def test_denied_reads_a_star_as_a_prefix_and_dot_star_as_a_module_and_those_under_it(self):
        for mod in ("Effect", "Effect.Ref", "Control.Monad.ST.Internal", "Data.Array.ST", "Data.Array.Partial",
                    "Unsafe.Coerce", "Partial.Unsafe", "Record.Unsafe", "Debug", "Debug.Trace"):
            with self.subTest(mod=mod):
                self.assertTrue(kernel.denied(mod))
        for mod in ("Prelude", "Data.Array", "Data.Eq", "Kernel.Tables", "Data.Array.NonEmpty", "Partial"):
            with self.subTest(mod=mod):
                self.assertFalse(kernel.denied(mod))

    def test_the_corefn_lint_names_a_foreign_declaration_and_a_denied_import(self):
        with tempfile.TemporaryDirectory() as out:
            d = Path(out) / "Kernel.Bad"
            d.mkdir()
            (d / "corefn.json").write_text(json.dumps({
                "moduleName": ["Kernel", "Bad"], "foreign": ["now"],
                "imports": [{"moduleName": ["Prelude"]}, {"moduleName": ["Effect", "Ref"]}]}))
            errors = kernel.lint_corefn(Path(out))
        self.assertEqual(len(errors), 2)
        self.assertIn("Kernel.Bad: foreign now", errors[0])
        self.assertIn("imports Effect.Ref, denied", errors[1])


def con(q: str) -> dict:
    *mod, name = q.split(".")
    return {"tag": "TypeConstructor", "contents": [mod, name]}


def app(f: dict, a: dict) -> dict:
    return {"tag": "TypeApp", "contents": [f, a]}


def record(*fields, rest=None) -> dict:
    row = rest or {"tag": "REmpty"}
    for label, t in reversed(fields):
        row = {"tag": "RCons", "contents": [label, t, row]}
    return app(con("Prim.Record"), row)


def fn(a: dict, b: dict) -> dict:
    return app(app(con("Prim.Function"), a), b)


class Build(unittest.TestCase):
    """kernel.d.ts from purs's docs.json: wire types only, or the build refuses (NotWire)."""

    def test_a_decision_on_a_closed_record_is_a_typed_function(self):
        t = fn(record(("phase", con("Prim.String")), ("at", con("Prim.Number"))),
               record(("tags", app(con("Prim.Array"), con("Prim.String"))), ("ok", con("Prim.Boolean"))))
        self.assertEqual(kernel.export_dts("decide", t, {}),
                         "export declare function decide(facts: {\n  readonly phase: string\n  readonly at: number\n}): {\n"
                         "  readonly tags: ReadonlyArray<string>\n  readonly ok: boolean\n}\n")

    def test_a_synonym_is_resolved_and_a_value_is_a_const(self):
        syn = {"Kernel.X.Facts": record(("n", con("Prim.Number")))}
        self.assertEqual(kernel.export_dts("x", con("Kernel.X.Facts"), syn),
                         "export declare const x: {\n  readonly n: number\n}\n")

    def test_int_maybe_an_open_row_a_curried_result_and_a_bare_argument_are_not_wire(self):
        cases = {
            "Prim.Int is not a wire type": fn(record(("n", con("Prim.Int"))), record()),
            "Data.Maybe.Maybe is not a wire type": record(("m", app(con("Data.Maybe.Maybe"), con("Prim.String")))),
            "an open row": record(("a", con("Prim.String")), rest={"tag": "TypeVar", "contents": "r"}),
            "Prim.Function is not a wire type": fn(record(), fn(record(), record())),
            "a decision takes one record of facts": fn(con("Prim.String"), record()),
            "Effect.Effect is not a wire type": app(con("Effect.Effect"), record()),
        }
        for want, t in cases.items():
            with self.subTest(want=want):
                with self.assertRaises(kernel.NotWire) as e:
                    kernel.export_dts("bad", t, {})
                self.assertIn(want, str(e.exception))


class Toolchain(unittest.TestCase):
    """fetch's checks: a bad hash, a link and a path escape are each refused by name."""

    def test_every_supported_platform_has_both_tools_pinned_with_both_hashes(self):
        pin = kernel.load_pin()
        for tool, digest in (("purs", "sha256"), ("esbuild", "sha512")):
            self.assertEqual(sorted(pin[tool]["assets"]), ["darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"])
            for key, entry in pin[tool]["assets"].items():
                with self.subTest(tool=tool, platform=key):
                    self.assertIn(digest, entry)
                    self.assertRegex(entry["bin_sha256"], r"^[0-9a-f]{64}$")
                    self.assertTrue(entry["url"].startswith(("https://github.com/purescript/purescript/releases/download/",
                                                             "https://registry.npmjs.org/@esbuild/")))
        self.assertEqual(pin["registry"], "https://packages.registry.purescript.org/")
        self.assertRegex(pin["spago"]["version"], r"^\d+\.\d+\.\d+$")

    def test_platform_key_maps_the_machine_names_to_the_pins_keys(self):
        for system, machine, want in (("Linux", "x86_64", "linux-x64"), ("Linux", "aarch64", "linux-arm64"),
                                      ("Darwin", "arm64", "darwin-arm64"), ("Darwin", "x86_64", "darwin-x64")):
            with self.subTest(want=want), mock.patch("platform.system", return_value=system), \
                    mock.patch("platform.machine", return_value=machine):
                self.assertEqual(kernel.platform_key(), want)

    def test_a_bad_hash_is_refused_by_name_for_each_kind_of_pin(self):
        for entry in ({"sha256": "0" * 64}, {"sha512": "sha512-AAAA"}, {"integrity": "sha256-AAAA"}, {}):
            with self.subTest(entry=entry), self.assertRaises(kernel.Refused) as e:
                kernel.check_hash(b"data", entry, "x.tgz")
            self.assertIn("bad hash", str(e.exception))
        kernel.check_hash(b"data", {"sha256": hashlib.sha256(b"data").hexdigest()}, "x.tgz")
        kernel.check_hash(b"data", {"integrity": kernel.sri("sha256", b"data")}, "x.tgz")

    def test_the_binary_must_be_a_regular_file_with_the_pinned_hash(self):
        good = tar_of([("purescript", "d", b""), ("purescript/purs", "f", b"binary")])
        self.assertEqual(kernel.verify_member(good, "purescript/purs", hashlib.sha256(b"binary").hexdigest()), b"binary")
        with self.assertRaises(kernel.Refused) as e:
            kernel.verify_member(good, "purescript/purs", "0" * 64)
        self.assertIn("bad hash", str(e.exception))
        link = tar_of([("purescript/purs", "l", b"/bin/sh")])
        with self.assertRaises(kernel.Refused) as e:
            kernel.verify_member(link, "purescript/purs", "0" * 64)
        self.assertIn("a link", str(e.exception))
        with self.assertRaises(kernel.Refused) as e:
            kernel.verify_member(good, "package/bin/esbuild", "0" * 64)
        self.assertIn("carries no package/bin/esbuild", str(e.exception))

    def test_unpack_refuses_a_link_a_path_escape_and_a_device_and_writes_nothing(self):
        cases = {
            "a link": [("p-1.0.0/src/A.purs", "f", b"x"), ("p-1.0.0/src/B.purs", "l", b"/etc/passwd")],
            "a link: 'p-1.0.0/h'": [("p-1.0.0/h", "h", b"p-1.0.0/x")],
            "a path escape": [("p-1.0.0/../../evil", "f", b"x")],
            "a path escape: '/abs'": [("/abs", "f", b"x")],
            "a path escape: 'other/x'": [("other/x", "f", b"x")],
            "not a regular file": [("p-1.0.0/dev", "c", b"")],
        }
        for want, members in cases.items():
            with self.subTest(want=want), tempfile.TemporaryDirectory() as dest:
                with self.assertRaises(kernel.Refused) as e:
                    kernel.unpack(tar_of(members), "p-1.0.0", Path(dest))
                self.assertIn(want, str(e.exception))
                self.assertEqual(list(Path(dest).iterdir()), [], "refused before anything is written")

    def test_unpack_writes_the_files_under_their_top_directory(self):
        with tempfile.TemporaryDirectory() as dest:
            kernel.unpack(tar_of([("p-1.0.0", "d", b""), ("p-1.0.0/src/A.purs", "f", b"module A where\n")]),
                          "p-1.0.0", Path(dest))
            self.assertEqual((Path(dest) / "p-1.0.0/src/A.purs").read_bytes(), b"module A where\n")

    def test_offline_a_missing_or_wrong_cached_copy_is_refused_naming_fetch(self):
        with tempfile.TemporaryDirectory() as into:
            with self.assertRaises(kernel.Refused) as e:
                kernel.ensure(into=into, network=False)
            self.assertIn("kernel.py fetch", str(e.exception))
            pin = kernel.load_pin()
            wrong = kernel.tool_path(Path(into), "purs", pin)
            wrong.parent.mkdir(parents=True)
            wrong.write_bytes(b"not purs")
            with self.assertRaises(kernel.Refused) as e:
                kernel.ensure(into=into, network=False)
            self.assertIn("missing or not the pinned one", str(e.exception))

    def test_the_cache_is_per_user_and_keyed_on_both_pin_files(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/x/cache"}):
            self.assertEqual(kernel.cache_dir(), Path("/x/cache/workbench/kernel") / kernel.pin_id())
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative"}):
            self.assertEqual(kernel.cache_base(), Path.home() / ".cache/workbench/kernel")
        self.assertEqual(kernel.cache_dir(into="/elsewhere"), Path("/elsewhere"))

    def test_spago_lock_pins_registry_packages_only_each_with_an_integrity(self):
        pkgs = kernel.locked_packages()
        self.assertIn("prelude", [n for n, _, _ in pkgs])
        for name, version, integrity in pkgs:
            with self.subTest(package=name):
                self.assertRegex(version, r"^\d+\.\d+\.\d+$")
                self.assertRegex(integrity, r"^sha256-[A-Za-z0-9+/]+=*$")


class Checker(unittest.TestCase):
    """Each way the boundary can break, made in a copy: each must fail by name."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-kernel-test-"))
        self.root = self.tmp / "tree"
        for d in ("kernel", "mod/hooks/golden"):
            shutil.copytree(ROOT / d, self.root / d)
        for f in ("kernel.js", "kernel.d.ts", "core.ts"):
            shutil.copy(ROOT / "mod/hooks" / f, self.root / "mod/hooks" / f)
        shutil.copytree(ROOT / "opencode-plugin", self.root / "opencode-plugin")
        self.assertEqual(kernel.offline_errors(self.root), [], "the copy starts clean")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def errors(self) -> str:
        return "\n".join(kernel.offline_errors(self.root))

    def edit(self, path: str, old: str, new: str) -> None:
        f = self.root / path
        text = f.read_text()
        self.assertIn(old, text)
        f.write_text(text.replace(old, new, 1))

    def test_a_hand_edit_to_the_bundle(self):
        self.edit("mod/hooks/kernel.js", "running: active && !needsClaude", "running: active")
        self.assertIn("mod/hooks/kernel.js was edited by hand", self.errors())

    def test_a_clock_in_the_bundle(self):
        self.edit("mod/hooks/kernel.js", "var phaseOf = function(v) {", "var phaseOf = function(v) { Date.now();")
        self.assertIn("mod/hooks/kernel.js holds Date.now", self.errors())

    def test_a_source_change_not_built(self):
        self.edit("kernel/src/Kernel/Phase.purs", "phaseOf { phase, parked } =", "phaseOf { phase, parked } =\n  -- changed\n ")
        self.assertIn("mod/hooks/kernel.js is stale", self.errors())
        self.assertIn("mod/hooks/kernel.d.ts is stale", self.errors())

    def test_effect_ref_imported(self):
        self.edit("kernel/src/Kernel/Phase.purs", "import Prelude\n", "import Prelude\nimport Effect.Ref (new)\n")
        self.assertIn("Phase.purs:6: imports Effect.Ref, denied", self.errors())

    def test_a_foreign_import(self):
        self.edit("kernel/src/Kernel/Phase.purs", "import Prelude\n", "import Prelude\nforeign import now :: Number\n")
        self.assertIn("a foreign import", self.errors())

    def test_locale_compare(self):
        self.edit("kernel/src/Kernel/Phase.purs", "-- | What a task's phase means", "-- | localeCompare What a task's phase means")
        self.assertIn("localeCompare is locale-dependent", self.errors())

    def test_a_changed_store_active(self):
        with mock.patch.object(store, "ACTIVE", {*store.ACTIVE, "paused"}):
            self.assertIn("Tables.purs is not what workbench's Python says", self.errors())

    def test_an_export_row_without_its_build_or_goldens(self):
        self.edit("kernel/BOUNDARY.md", "| `phaseOf` |", "| `openCounts` | Kernel.Counts | counts | born:#1325 | core.ts |\n| `phaseOf` |")
        self.assertIn("the exports differ", self.errors())

    def test_a_golden_for_no_export(self):
        self.edit("mod/hooks/golden/phase.ts", "fn: 'phaseOf', in: { phase: 'queued'", "fn: 'phase', in: { phase: 'queued'")
        self.assertIn("the exports differ", self.errors())

    def test_a_golden_file_neither_host_runs(self):
        shutil.copy(self.root / "mod/hooks/golden/phase.ts", self.root / "mod/hooks/golden/phase2.ts")
        self.assertIn("golden/phase2.ts is not in golden/index.ts's GOLDENS", self.errors())

    def test_a_second_importer_of_the_kernel(self):
        (self.root / "opencode-plugin/index.ts").write_text("import * as K from '../mod/hooks/kernel.js'\n")
        self.assertIn("opencode-plugin/index.ts imports kernel.js: only mod/hooks/core.ts does", self.errors())

    def test_a_phase_literal_in_the_plugin(self):
        (self.root / "opencode-plugin/notices.ts").write_text("export const due = (p: string) => p === 'review'\n")
        self.assertIn("opencode-plugin/notices.ts:1: a phase literal", self.errors())

    def test_a_replaced_name_still_defined(self):
        self.edit("kernel/BOUNDARY.md", "| born:#1356 | core.ts; register.tsx and tasks.ts from #1325's M1 |\n| `phaseOf`",
                  "| mod/hooks/core.ts:ACTIVE | core.ts |\n| `phaseOf`")
        self.assertIn("mod/hooks/core.ts:ACTIVE is still defined", self.errors())

    def test_an_export_over_budget(self):
        self.edit("kernel/BOUNDARY.md", "| exports | 12 |", "| exports | 1 |")
        self.assertIn("budget: 2 exports, over 1", self.errors())

    def test_a_module_over_its_lines(self):
        self.edit("kernel/BOUNDARY.md", "| module lines | 350 |", "| module lines | 10 |")
        self.assertIn("budget: kernel/src/Kernel/Phase.purs is", self.errors())

    def test_no_banner(self):
        f = self.root / "mod/hooks/kernel.d.ts"
        f.write_bytes(f.read_bytes().partition(b"\n")[2])
        self.assertIn("mod/hooks/kernel.d.ts has no banner on its first line", self.errors())


if __name__ == "__main__":
    unittest.main()
