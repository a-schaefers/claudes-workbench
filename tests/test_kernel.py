"""The PureScript kernel's boundary, held offline (kernel/BOUNDARY.md, #1356): no toolchain needed,
so `python3 -m unittest` runs it for a TypeScript, Python or docs change. CI's kernel job runs
`scripts/kernel.py check`, which rebuilds the bundle with the pinned toolchain on top of this.

Each failure names its fix. The Checker class breaks a copy of the tree in each way a check must
catch (never the tree itself) and asserts that the break fails by name."""
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
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
from workbench import spend, store  # noqa: E402

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

    def test_only_tests_and_goldens_take_core_ts_untyped_kernel_record(self):
        self.assertEqual(kernel.core_record_importers(), [])


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
        self.assertEqual(v["working"], sorted(store.WORKING))
        self.assertEqual(v["stopped"], sorted(store.STOPPED))
        self.assertEqual(v["noAnswer"], "No answer arrived in time.")
        self.assertIsInstance(v["chatStaleMs"], float)
        self.assertEqual(v["wakeOpening"], spend.WAKE_OPENING)

    def test_open_order_keeps_its_order_and_names_every_open_phase_once(self):
        # the pane's header reads its counts in this order (#1325's M1): a sorted copy would put
        # asking before running
        v = kernel.tables_values()
        self.assertEqual(v["openOrder"], list(store.OPEN_ORDER))
        self.assertEqual(sorted(v["openOrder"]), sorted(store.ACTIVE | store.NEEDS_CLAUDE))
        self.assertEqual(set(store.WORKING) <= store.ACTIVE, True)
        self.assertEqual(set(store.STOPPED) <= set(store.PARKS), True)

    def test_spends_wake_pattern_is_built_from_the_opening_the_mod_writes(self):
        # the mod's wakes start with core.ts's WAKE_OPENING, from Tables, from spend.WAKE_OPENING:
        # one string, so a reworded wake can't stop charging turns to their task (#557, #1356)
        self.assertEqual(spend.WAKE.match(f"{spend.WAKE_OPENING} `fix-it` is ready").group(1), "fix-it")
        self.assertEqual(spend.WAKE.match("The workbench plugin sent a message: [workbench] Task `a-b` x").group(1), "a-b")
        self.assertIsNone(spend.WAKE.match("[workbench]xTask `a` x"))

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
                "moduleName": ["Kernel", "Bad"], "modulePath": "src/Kernel/Bad.purs", "foreign": ["now"],
                "imports": [{"moduleName": ["Prelude"]}, {"moduleName": ["Effect", "Ref"]}]}))
            errors = kernel.lint_corefn(Path(out))
        self.assertEqual(len(errors), 2)
        self.assertIn("Kernel.Bad: foreign now", errors[0])
        self.assertIn("imports Effect.Ref, denied", errors[1])

    def test_the_corefn_lint_reads_every_kernel_src_module_whatever_its_name_and_no_package(self):
        # the 2026-11-25 review: only Kernel.* was read, so src/Helper/Clock.purs's Effect.Unsafe
        # reached the bundle with lint, build and check all passing
        def corefn(out, name, path, imports, foreign=()):
            d = Path(out) / name
            d.mkdir()
            (d / "corefn.json").write_text(json.dumps({
                "moduleName": name.split("."), "modulePath": path, "foreign": list(foreign),
                "imports": [{"moduleName": m.split(".")} for m in imports]}))
        with tempfile.TemporaryDirectory() as out:
            corefn(out, "Helper.Clock", "src/Helper/Clock.purs", ["Prelude", "Effect.Unsafe"])
            corefn(out, "Effect.Unsafe", "packages/effect-4.0.0/src/Effect/Unsafe.purs", ["Effect"], ["unsafePerformEffect"])
            errors = kernel.lint_corefn(Path(out))
        self.assertEqual(errors, ["Helper.Clock: imports Effect.Unsafe, denied (kernel/BOUNDARY.md, *Denied imports*)"])

    def test_the_source_lint_reads_an_import_whose_module_name_is_on_the_next_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "kernel" / "src" / "Helper"
            src.mkdir(parents=True)
            (src / "Clock.purs").write_text("module Helper.Clock where\n\nimport Prelude\nimport\n  Effect.Unsafe (unsafePerformEffect)\n"
                                            "foreign\n  import now :: Number\n")
            errors = kernel.lint_sources(Path(tmp))
        self.assertEqual(errors, ["kernel/src/Helper/Clock.purs:6: a foreign import; kernel/src has no FFI (kernel/BOUNDARY.md)",
                                  "kernel/src/Helper/Clock.purs:4: imports Effect.Unsafe, denied (kernel/BOUNDARY.md, *Denied imports*)"])

    def test_a_comment_inside_an_import_or_a_foreign_import_hides_neither_from_the_source_lint(self):
        # the 2026-11-25 adversarial review: `import {- -} Effect.Unsafe` passed the offline lint,
        # and only kernel.py's corefn lint, which needs the toolchain, caught it
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "kernel" / "src" / "Helper"
            src.mkdir(parents=True)
            (src / "Clock.purs").write_text(
                "module Helper.Clock where\n\nimport Prelude\nimport {- {- nested -} -} Effect.Unsafe (unsafePerformEffect)\n"
                "{- a -} import -- why\n  Unsafe.Coerce (unsafeCoerce)\nimport Data.Array ((-->))\n"
                "foreign {- x -} import now :: Number\n")
            errors = kernel.lint_sources(Path(tmp))
        self.assertEqual(errors, ["kernel/src/Helper/Clock.purs:8: a foreign import; kernel/src has no FFI (kernel/BOUNDARY.md)",
                                  "kernel/src/Helper/Clock.purs:4: imports Effect.Unsafe, denied (kernel/BOUNDARY.md, *Denied imports*)",
                                  "kernel/src/Helper/Clock.purs:5: imports Unsafe.Coerce, denied (kernel/BOUNDARY.md, *Denied imports*)"])
        self.assertEqual(kernel.uncommented("a -- b\nc {- d\n-} e --> f"), "a     \nc     \n   e --> f")


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

    def test_a_failed_compile_shows_purss_error_not_its_progress(self):
        # purs prints errors on stdout and "[n of m] Compiling" on stderr: the refusal showed only
        # the progress, never what was wrong (the 2026-11-25 review)
        prog = ("import sys; print('Error found:\\nin module Kernel.Bad'); "
                "[print(f'[{i} of 99] Compiling M{i}', file=sys.stderr) for i in range(99)]; sys.exit(1)")
        with tempfile.TemporaryDirectory() as cwd, self.assertRaises(kernel.Refused) as e:
            kernel._run([sys.executable, "-I", "-c", prog], Path(cwd), "purs compile")
        self.assertIn("purs compile failed (exit 1):\nError found:\nin module Kernel.Bad", str(e.exception))
        self.assertNotIn("Compiling", str(e.exception))
        with tempfile.TemporaryDirectory() as cwd, self.assertRaises(kernel.Refused) as e:
            kernel._run([sys.executable, "-I", "-c", "import sys; print('x.js: error: bad', file=sys.stderr); sys.exit(1)"],
                        Path(cwd), "esbuild")
        self.assertIn("esbuild failed (exit 1):\nx.js: error: bad", str(e.exception))

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


class Props(unittest.TestCase):
    """`kernel.py props`: Props.purs's seeded properties under Bun, run by `check` (#1356)."""

    def test_props_run_on_bun_else_opencodes_bun_and_refuse_by_name_with_neither(self):
        def which(found):
            return lambda name: f"/bin/{name}" if name in found else None
        with mock.patch("shutil.which", which({"bun", "opencode"})):
            self.assertEqual(kernel.bun(), ("/bin/bun", {}))
        with mock.patch("shutil.which", which({"opencode"})):
            self.assertEqual(kernel.bun(), ("/bin/opencode", {"BUN_BE_BUN": "1"}))
        with mock.patch("shutil.which", which(set())), self.assertRaises(kernel.Refused) as e:
            kernel.props(into="/nonexistent")
        self.assertIn("props need Bun: neither bun nor opencode", str(e.exception))

    def test_props_test_runs_the_plans_seeds_and_count_within_ten_seconds(self):
        text = (kernel.paths()["test"] / kernel.PROPS_TEST).read_text()
        self.assertIn("const SEEDS = [20261124, 1, 2]", text)
        self.assertIn("const N = 1000", text)
        self.assertIn("}, 10_000)", text)
        self.assertIn("module Props (props) where", (kernel.paths()["test"] / "Props.purs").read_text())

    def test_check_runs_the_props(self):
        with mock.patch.object(kernel, "offline_errors", return_value=[]), \
                mock.patch.object(kernel, "ensure", return_value=Path("/c")), \
                mock.patch.object(kernel, "compile_kernel", return_value=(b"js", b"dts")), \
                mock.patch.object(kernel, "budget_errors", return_value=[]), \
                mock.patch.object(kernel, "props", side_effect=kernel.Refused("props failed (exit 1):\nseed 1 case 7")):
            self.assertIn("props failed (exit 1):\nseed 1 case 7", kernel.check(say=lambda _: None))


def golden_dump() -> list:
    """Every golden, as Bun reads golden/index.ts: the cases are TypeScript, so Bun (bun, or
    OpenCode's) evaluates them rather than a parser here."""
    runner, env = kernel.bun()
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "dump.ts"
        index = ROOT / "mod" / "hooks" / "golden" / "index.ts"
        script.write_text(f"import {{ GOLDENS }} from {json.dumps(str(index))}\nconsole.log(JSON.stringify(GOLDENS))\n")
        r = subprocess.run([runner, "run", str(script)], cwd=tmp, env={**os.environ, **env},
                           capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise AssertionError(f"Bun could not read the goldens: {r.stderr[-2000:]}")
    return json.loads(r.stdout)


def twin(name: str):
    """A golden case's `twin`: `kernel.<name>` is scripts/kernel.py's, `<mod>.<name>` workbench's."""
    mod, _, attr = name.rpartition(".")
    return getattr(kernel if mod == "kernel" else importlib.import_module(f"workbench.{mod}"), attr)


def twin_failures(goldens: list) -> list[str]:
    out = []
    for g in goldens:
        for i, c in enumerate(g["cases"], 1):
            if "twin" not in c:
                continue
            f = twin(c["twin"])
            got = (f() if callable(f) else f) if c["in"] is None else f(**c["in"])
            if got != c["out"]:
                out.append(f"{g['module']} #{i} {c['fn']} ({c['why']}): its twin {c['twin']} gave {got!r}, the golden says {c['out']!r}")
    return out


class Twins(unittest.TestCase):
    """Each golden case with a `twin` holds on that Python name too, called with the case's `in` as
    keyword arguments (a value export's twin with none): the kernel and wb's own code give one
    answer (kernel/BOUNDARY.md, *Move protocol*)."""

    @classmethod
    def setUpClass(cls):
        try:
            kernel.bun()
        except kernel.Refused:
            raise unittest.SkipTest("the goldens are TypeScript: Bun reads them (bun, or OpenCode's)") from None
        cls.goldens = golden_dump()

    def test_every_golden_with_a_twin_holds_on_its_python_twin(self):
        self.assertIn("kernel.tables_values", [c.get("twin") for g in self.goldens for c in g["cases"]])
        self.assertEqual(twin_failures(self.goldens), [])

    def test_a_golden_its_twin_disagrees_with_fails_naming_both_answers(self):
        wrong = [{"module": "Kernel.Tables", "cases": [{"fn": "tables", "in": None, "why": "stale", "twin": "kernel.tables_values",
                                                        "out": {**kernel.tables_values(), "chatStaleMs": 1}}]}]
        got = twin_failures(wrong)
        self.assertEqual(len(got), 1)
        self.assertIn("Kernel.Tables #1 tables (stale): its twin kernel.tables_values gave", got[0])
        self.assertIn("'chatStaleMs': 1,", got[0])


class Checker(unittest.TestCase):
    """Each way the boundary can break, made in a copy: each must fail by name."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-kernel-test-"))
        self.root = self.tmp / "tree"
        for d in ("kernel", "mod/hooks/golden"):
            shutil.copytree(ROOT / d, self.root / d)
        for f in ("kernel.js", "kernel.d.ts", "core.ts", "register.tsx", "tasks.ts"):
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
        self.assertIn("Phase.purs:7: imports Effect.Ref, denied", self.errors())

    def test_a_helper_module_importing_an_effect_across_two_lines(self):
        (self.root / "kernel/src/Helper").mkdir()
        (self.root / "kernel/src/Helper/Clock.purs").write_text(
            "module Helper.Clock (sneaky) where\n\nimport\n  Effect.Unsafe (unsafePerformEffect)\n")
        self.assertIn("kernel/src/Helper/Clock.purs:3: imports Effect.Unsafe, denied", self.errors())

    def test_an_effect_in_the_props(self):
        self.edit("kernel/test/Props.purs", "import Prelude\n", "import Prelude\nimport Effect.Console (log)\n")
        self.assertIn("kernel/test/Props.purs:8: imports Effect.Console, denied", self.errors())

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
        self.edit("kernel/BOUNDARY.md", "| `phaseOf` |", "| `gone` | Kernel.Gone | gone | born:#1325 | core.ts |\n| `phaseOf` |")
        self.assertIn("the exports differ", self.errors())

    def test_a_golden_for_no_export(self):
        self.edit("mod/hooks/golden/phase.ts", "fn: 'phaseOf', in: t('queued')", "fn: 'phase', in: t('queued')")
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

    def test_a_require_or_an_mts_jsx_or_cjs_importer_of_the_kernel(self):
        # the membrane scan read .ts, .tsx, .js and .mjs only, and no require() (#1356's review)
        for name, text in (("x.mts", "import * as K from '../mod/hooks/kernel.js'\n"),
                           ("y.cjs", "const K = require('../mod/hooks/kernel.js')\n"),
                           ("z.jsx", "const K = require(\"../mod/hooks/kernel\")\n"),
                           ("w.cts", "import K = require('../mod/hooks/kernel.js')\n")):
            with self.subTest(file=name):
                f = self.root / "opencode-plugin" / name
                f.write_text(text)
                self.assertIn(f"opencode-plugin/{name} imports kernel.js", self.errors())
                f.unlink()

    def test_a_host_file_taking_core_ts_untyped_kernel_record(self):
        # the hosts take core.ts's typed names; its `kernel` record (every export, untyped) is the
        # goldens' (#1356's review)
        for text in ("import { ACTIVE, kernel } from './core'\n", "import * as C from './core.js'\n",
                     "const C = require('./core')\n", "export { kernel } from './core'\n"):
            with self.subTest(text=text):
                f = self.root / "mod/hooks/notices.ts"
                f.write_text(text)
                self.assertIn("mod/hooks/notices.ts imports core.ts's untyped kernel record", self.errors())
        f.write_text("import { ACTIVE, phaseOf } from './core'\n")
        self.assertNotIn("untyped kernel record", self.errors())
        (self.root / "mod/hooks/notices.test.ts").write_text("import { kernel } from './core.js'\n")
        (self.root / "mod/hooks/golden/runner2.ts").write_text("import { kernel } from '../core.js'\n")
        self.assertNotIn("untyped kernel record", self.errors())

    def test_a_phase_set_back_in_the_mods_hooks(self):
        # register.tsx's RUNNING and tasks.ts's WORKING and COUNTED were hand copies of phase sets
        # the kernel holds (#1325's M1): two phases on a line, or in one list across lines
        self.edit("mod/hooks/register.tsx", "\nexport function attachedLines", "\nconst LIVE = new Set(['queued', 'running'])\nexport function attachedLines")
        self.assertRegex(self.errors(), r"mod/hooks/register.tsx:\d+: a phase literal")
        self.edit("mod/hooks/register.tsx", "const LIVE = new Set(['queued', 'running'])", "const LIVE = new Set([\n  'queued',\n  'running',\n])")
        self.assertRegex(self.errors(), r"mod/hooks/register.tsx:\d+: a phase literal")
        self.edit("mod/hooks/register.tsx", "const LIVE = new Set([\n  'queued',\n  'running',\n])", "const done = (p: string) => p === 'accepted' || p === 'discarded'")
        self.assertRegex(self.errors(), r"mod/hooks/register.tsx:\d+: a phase literal")

    def test_one_phase_alone_in_the_mods_hooks_is_its_own_words(self):
        self.edit("mod/hooks/tasks.ts", "\nexport const ago", "\nconst said = (p: string) => p === 'review' ? 'awaiting review' : p\nexport const ago")
        self.assertNotIn("a phase literal", self.errors())

    def test_a_replaced_name_still_defined(self):
        self.edit("kernel/BOUNDARY.md", "mod/hooks/tasks.ts:CLOSED | core.ts;", "mod/hooks/tasks.ts:CLOSED, mod/hooks/core.ts:ACTIVE | core.ts;")
        self.assertIn("mod/hooks/core.ts:ACTIVE is still defined", self.errors())

    def test_a_hand_copy_of_a_table_back_in_the_mod(self):
        self.edit("mod/hooks/register.tsx", " NEEDS, NO_ANSWER,", " NO_ANSWER,")
        self.edit("mod/hooks/register.tsx", "\nexport function attachedLines", "\nconst NEEDS = new Set(['review', 'stuck', 'failed', 'asking'])\nexport function attachedLines")
        self.assertIn("mod/hooks/register.tsx:NEEDS is still defined", self.errors())
        self.edit("mod/hooks/tasks.ts", "import { CLOSED, openCounts, phaseOf } from './core'\n",
                  "import { openCounts, phaseOf } from './core'\nexport const CLOSED = new Set(['accepted', 'discarded'])\n")
        self.assertIn("mod/hooks/tasks.ts:CLOSED is still defined", self.errors())

    def test_an_export_over_budget(self):
        self.edit("kernel/BOUNDARY.md", "| exports | 12 |", "| exports | 1 |")
        self.assertRegex(self.errors(), r"budget: \d+ exports, over 1")

    def test_a_module_over_its_lines(self):
        self.edit("kernel/BOUNDARY.md", "| module lines | 350 |", "| module lines | 10 |")
        self.assertIn("budget: kernel/src/Kernel/Phase.purs is", self.errors())

    def test_no_banner(self):
        f = self.root / "mod/hooks/kernel.d.ts"
        f.write_bytes(f.read_bytes().partition(b"\n")[2])
        self.assertIn("mod/hooks/kernel.d.ts has no banner on its first line", self.errors())


if __name__ == "__main__":
    unittest.main()
