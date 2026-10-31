"""Fake-GH tests: no real network, credentials, installed skill edits or lock normalization."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROFILE=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader("gh_skills_verified",str(PROFILE/"bin/topgrade-gh-skills-verified"))
spec=importlib.util.spec_from_loader(loader.name,loader)
helper=importlib.util.module_from_spec(spec)
loader.exec_module(helper)
OLD="1"*40
NEW="2"*40
ROOT_SHA="3"*40
BLOB="4"*40
COMMIT="5"*40
TAG="6"*40

def skill(path,name,github=False,tree=OLD,directory=None):
    path.mkdir(parents=True,exist_ok=True)
    header=f"---\nname: {name}\ndescription: test\n"
    if github:
        header+=("metadata:\n  github-repo: https://github.com/example/skills\n"
                 f"  github-path: {directory if directory is not None else 'skills/'+name}\n"
                 f"  github-tree-sha: {tree}\n")
    (path/"SKILL.md").write_text(header+"---\nunchanged test body\n")

class FakeGH:
    def __init__(self,rows):
        self.rows=rows
        self.calls=[]
        self.entries=[]
        self.output=None
        self.failure=0
        self.write=True
        self.truncated=False
        self.api_failure=False
        self.release_status=404
        self.release={"tag_name":"v1"}
        self.default_branch="main"
        self.annotated=False
        self.home=None
        self.record_write=True
        self.clobber_lock=False
        self.mutate_dry_lock=False
        for row in rows:
            if row["sourceURL"]:
                name=Path(row["path"]).name
                self.entries.extend([
                    {"path":"skills/"+name,"type":"tree","sha":NEW,"mode":"040000"},
                    {"path":"skills/"+name+"/SKILL.md","type":"blob","sha":BLOB,"mode":"100644"}])

        self.entries=list({entry["path"]:entry for entry in self.entries}.values())

    def __call__(self,args,**kwargs):
        self.calls.append(args)
        body=""
        err=""
        code=0
        if args[1:3]==["skill","list"]:
            body=json.dumps(self.rows)
        elif args[1]=="api":
            if self.api_failure:
                code,err=1,"PRIVATE_TOKEN_SENTINEL"
            else:
                route=args[-1]
                status=200
                if route.endswith("/releases/latest"):
                    status=self.release_status
                    value=self.release
                elif route=="repos/example/skills":
                    value={"default_branch":self.default_branch}
                elif "/git/ref/" in route:
                    value={"object":{"sha":TAG if self.annotated else COMMIT,
                                     "type":"tag" if self.annotated else "commit"}}
                elif "/git/tags/" in route:
                    assert route.endswith(TAG)
                    value={"object":{"sha":COMMIT,"type":"commit"}}
                else:
                    assert route.endswith("/git/trees/"+COMMIT+"?recursive=1"),route
                    value={"sha":ROOT_SHA,"tree":self.entries,"truncated":self.truncated}
                body=f"HTTP/2.0 {status} status\r\n\r\n"+json.dumps(value)
                code=int(status!=200)
        elif args[1:3]==["skill","update"]:
            parent=Path(args[args.index("--dir")+1])
            names=args[args.index("--all")+1:]
            dry="--dry-run" in names
            names=[name for name in names if name!="--dry-run"]
            for name in names:
                assert "/" not in name and not name.startswith("-"),"Expected a literal directory basename"
            if self.output is not None:
                body=self.output
            elif dry:
                if self.mutate_dry_lock:
                    path=self.home/".agents/.skill-lock.json"
                    value=json.loads(path.read_text())
                    value["dismissed"]={"preserve-choice":False}
                    path.write_text(json.dumps(value))
                body=f"\n{len(names)} update(s) available:\n"
            elif self.write:
                lock_path=self.home/".agents/.skill-lock.json"
                value=json.loads(lock_path.read_text())
                for name in names:
                    path=parent/name/"SKILL.md"
                    text=path.read_text()
                    target=ROOT_SHA if "github-path: .\n" in text else NEW
                    path.write_text(text.replace(OLD,target))
                    if self.record_write:
                        key=next(row["skillName"] for row in self.rows if Path(row["path"])==parent/name)
                        value["skills"][key]={"source":"example/skills","sourceType":"github",
                            "sourceUrl":"https://github.com/example/skills.git",
                            "skillPath":"SKILL.md" if target==ROOT_SHA else "skills/"+name+"/SKILL.md",
                            "skillFolderHash":target,"installedAt":"2026-10-31T00:00:00Z","updatedAt":"2026-10-31T00:00:00Z"}
                if self.record_write:
                    if self.clobber_lock:
                        value["skills"].pop("unrelated",None)
                    lock_path.write_text(json.dumps(value))
                body="".join(f"Updated {name}\n" for name in names)
            else:
                body="All skills are up to date.\n"
            code=self.failure
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args,code,body,err)

    def updates(self):
        return [args for args in self.calls if args[1:3]==["skill","update"]]

class GhSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.shared=self.home/".agents/skills"
        self.rows=[]
        self.records={}
        self.write_lock()

    def row(self,path,name,source=""):
        row={"path":str(path),"skillName":name,"sourceURL":source,"pinned":False,"scope":"user"}
        self.rows.append(row)
        return row

    def github(self,name="managed",parent=None,alias=None,tree=OLD,directory=None):
        path=(parent or self.shared)/name
        skill(path,name,True,tree,directory)
        self.row(path,alias or "namespace/"+name,"https://github.com/example/skills")
        return path

    def npx(self,name):
        path=self.shared/name
        skill(path,name)
        self.row(path,name)
        self.records[name]={"source":"example/skills","sourceType":"github",
            "sourceUrl":"https://github.com/example/skills.git",
            "skillPath":"skills/"+name+"/SKILL.md","skillFolderHash":OLD,
            "installedAt":"2026-10-31T00:00:00Z","updatedAt":"2026-10-31T00:00:00Z"}
        return path

    def write_lock(self):
        path=self.home/".agents/.skill-lock.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({"version":3,"skills":self.records,"dismissed":{"preserve-choice":True}}))
        return path

    def run_helper(self,fake,dry=False):
        fake.home=self.home
        return helper.update(self.home,"fake-gh",run=fake,dry_run=dry)

    def test_all_176_rows_route_to_existing_owners_without_metadata_rewrites(self):
        for number in range(100):
            self.github(f"managed{number:03d}")
        self.npx("lean-ctx")
        for number in range(46):
            self.npx(f"npx{number:02d}")
        for root in (".cursor/skills",".copilot/skills",".config/opencode/skills"):
            path=self.home/root/"lean-ctx"
            skill(path,"lean-ctx")
            self.row(path,"lean-ctx")
        cursor=helper.sibling("topgrade-cursor-native-reconcile")
        for name in cursor.NAMES:
            skill(self.home/".cursor/skills-cursor"/name,name)
            path=self.shared/name
            skill(path,name)
            self.row(path,name)
        lock=self.write_lock()
        before_lock=lock.read_bytes()
        before_npx=(self.shared/"lean-ctx/SKILL.md").read_bytes()
        fake=FakeGH(self.rows)
        result=self.run_helper(fake)
        self.assertEqual((result["gh"],result["npx_delegated"],result["cursor_delegated"]),(100,50,26))
        self.assertEqual(len(fake.updates()),1)
        self.assertEqual(sum(args[1]=="api" for args in fake.calls),4)
        self.assertTrue(all(args[2:4]==["--hostname","github.com"] for args in fake.calls if args[1]=="api"))
        self.assertEqual(len(fake.updates()[0][fake.updates()[0].index("--all")+1:]),100)
        after_lock=json.loads(lock.read_text())
        original=json.loads(before_lock)
        self.assertEqual(after_lock["version"],original["version"])
        self.assertEqual(after_lock["dismissed"],original["dismissed"])
        self.assertTrue(all(after_lock["skills"].get(key)==value for key,value in original["skills"].items()))
        self.assertEqual(len(after_lock["skills"])-len(original["skills"]),100)
        self.assertEqual((self.shared/"lean-ctx/SKILL.md").read_bytes(),before_npx)

    def test_dry_run_reports_independent_plan_without_header_or_lock_writes(self):
        path=self.github()
        lock=self.write_lock()
        before=(path/"SKILL.md").read_bytes(),lock.read_bytes()
        fake=FakeGH(self.rows)
        result=self.run_helper(fake,True)
        self.assertEqual((result["status"],result["planned"],result["current_at_preflight"]),("PLAN",1,0))
        self.assertIn("--dry-run",fake.updates()[0])
        self.assertEqual(((path/"SKILL.md").read_bytes(),lock.read_bytes()),before)

    def test_same_basename_in_distinct_roots_uses_exclusive_groups(self):
        self.github(parent=self.home/"first")
        self.github(parent=self.home/"second")
        fake=FakeGH(self.rows)
        self.run_helper(fake)
        self.assertEqual(len(fake.updates()),2)
        self.assertTrue(all(args[-1]=="managed" and "namespace/managed" not in args for args in fake.updates()))

    def test_unknown_owner_pinned_or_bad_metadata_stops_before_updates(self):
        path=self.github()
        self.row(self.shared/"unknown","unknown")
        skill(self.shared/"unknown","unknown")
        fake=FakeGH(self.rows)
        with self.assertRaisesRegex(RuntimeError,"No verified updater"):
            self.run_helper(fake)
        self.assertEqual(fake.updates(),[])
        self.rows.pop()
        self.rows[0]["pinned"]=True
        with self.assertRaises(RuntimeError):
            self.run_helper(FakeGH(self.rows))
        self.rows[0]["pinned"]=False
        (path/"SKILL.md").write_text("---\nname: managed\n---\n")
        with self.assertRaisesRegex(RuntimeError,"metadata"):
            self.run_helper(FakeGH(self.rows))

    def test_npx_source_drift_and_mirror_drift_are_rejected(self):
        canonical=self.npx("lean-ctx")
        copy=self.home/".cursor/skills/lean-ctx"
        skill(copy,"lean-ctx")
        self.row(copy,"lean-ctx")
        self.write_lock()
        (copy/"extra").write_text("foreign")
        with self.assertRaisesRegex(RuntimeError,"copy differs"):
            self.run_helper(FakeGH(self.rows))
        (copy/"extra").unlink()
        self.records["lean-ctx"]["sourceUrl"]="https://github.com/other/publisher"
        self.write_lock()
        with self.assertRaisesRegex(RuntimeError,"No verified updater"):
            self.run_helper(FakeGH(self.rows))

    def test_cursor_vendor_drift_is_not_delegated(self):
        cursor=helper.sibling("topgrade-cursor-native-reconcile")
        name=cursor.NAMES[0]
        skill(self.shared/name,name)
        skill(self.home/".cursor/skills-cursor"/name,name)
        (self.shared/name/"extra").write_text("foreign")
        self.row(self.shared/name,name)
        with self.assertRaisesRegex(RuntimeError,"Cursor native"):
            self.run_helper(FakeGH(self.rows))

    def test_missing_remote_skill_path_or_truncated_tree_stops_before_updates(self):
        self.github()
        self.github("another")
        for mode in ("missing","truncated","file-as-directory"):
            fake=FakeGH(self.rows)
            if mode=="missing":
                fake.entries=[]
            elif mode=="truncated":
                fake.truncated=True
            else:
                text=(self.shared/"managed/SKILL.md").read_text()
                (self.shared/"managed/SKILL.md").write_text(text.replace("github-path: skills/managed\n","github-path: skills/managed/SKILL.md\n"))
            with self.subTest(mode=mode),self.assertRaises(RuntimeError) as error:
                self.run_helper(fake)
            self.assertEqual(fake.updates(),[])
            if mode=="missing":
                self.assertIn("skills/managed/SKILL.md",str(error.exception))
                self.assertIn("skills/another/SKILL.md",str(error.exception))

    def test_root_skill_uses_verified_root_tree_sha(self):
        path=self.github(directory=".")
        fake=FakeGH(self.rows)
        fake.entries=[{"path":"SKILL.md","type":"blob","sha":BLOB,"mode":"100644"}]
        self.run_helper(fake)
        self.assertIn(ROOT_SHA,(path/"SKILL.md").read_text())

    def test_warning_unknown_summary_and_native_failure_never_pass(self):
        self.github()
        for output,code in (("All skills are up to date.\n! warning",0),
                            ("no installed skills found",0),("Updated managed\n",9)):
            fake=FakeGH(self.rows)
            fake.output,fake.failure=output,code
            with self.subTest(output=output),self.assertRaises(RuntimeError):
                self.run_helper(fake)

    def test_current_summary_requires_already_verified_remote_metadata(self):
        self.github(tree=NEW)
        fake=FakeGH(self.rows)
        fake.write=False
        result=self.run_helper(fake)
        self.assertEqual((result["status"],result["planned"],result["current_at_preflight"]),("VERIFIED",0,1))

    def test_latest_release_uses_tag_commit_instead_of_head_without_metadata_changes(self):
        path=self.github(tree=NEW)
        file=path/"SKILL.md"
        file.write_text(file.read_text().replace("metadata:\n","metadata:\n  github-ref: refs/tags/v1\n"))
        before=file.read_bytes()
        fake=FakeGH(self.rows)
        fake.release_status=200
        fake.write=False
        result=self.run_helper(fake)
        self.assertEqual(result["planned"],0)
        self.assertEqual(file.read_bytes(),before)
        routes=[args[-1] for args in fake.calls if args[1]=="api"]
        self.assertIn("repos/example/skills/git/ref/tags/v1",routes)
        self.assertIn("repos/example/skills/git/trees/"+COMMIT+"?recursive=1",routes)
        self.assertNotIn("repos/example/skills",routes)
        self.assertFalse(any("HEAD" in route for route in routes))

    def test_annotated_release_tag_is_dereferenced_once(self):
        self.github()
        fake=FakeGH(self.rows)
        fake.release_status=200
        fake.release={"tag_name":"release/v1"}
        fake.annotated=True
        self.run_helper(fake,True)
        routes=[args[-1] for args in fake.calls if args[1]=="api"]
        self.assertIn("repos/example/skills/git/ref/tags/release%2Fv1",routes)
        self.assertEqual(routes.count("repos/example/skills/git/tags/"+TAG),1)

    def test_default_branch_only_follows_no_release_or_empty_release_tag(self):
        self.github()
        for status,release in ((404,{"message":"Not Found"}),(200,{"tag_name":""}),(200,{})):
            fake=FakeGH(self.rows)
            fake.release_status=status
            fake.release=release
            fake.default_branch="release/stable"
            with self.subTest(status=status,release=release):
                self.run_helper(fake,True)
            routes=[args[-1] for args in fake.calls if args[1]=="api"]
            self.assertIn("repos/example/skills",routes)
            self.assertIn("repos/example/skills/git/ref/heads/release%2Fstable",routes)

    def test_release_api_errors_or_invalid_reference_never_fall_back(self):
        self.github()
        for status,release in ((403,{}),(500,{}),(200,{"tag_name":None})):
            fake=FakeGH(self.rows)
            fake.release_status=status
            fake.release=release
            with self.subTest(status=status,release=release),self.assertRaises(RuntimeError):
                self.run_helper(fake)
            self.assertEqual(fake.updates(),[])
            self.assertEqual(len([args for args in fake.calls if args[1]=="api"]),1)
        fake=FakeGH(self.rows)
        fake.default_branch=""
        with self.assertRaisesRegex(RuntimeError,"default branch"):
            self.run_helper(fake)
        self.assertEqual(fake.updates(),[])

    def test_native_zero_without_expected_metadata_update_fails(self):
        self.github()
        fake=FakeGH(self.rows)
        fake.output="Updated managed\n"
        with self.assertRaisesRegex(RuntimeError,"verification failed"):
            self.run_helper(fake)

    def test_api_errors_do_not_expose_response_or_authentication_values(self):
        self.github()
        fake=FakeGH(self.rows)
        fake.api_failure=True
        with self.assertRaises(RuntimeError) as result:
            self.run_helper(fake)
        self.assertNotIn("PRIVATE_TOKEN_SENTINEL",str(result.exception))
        self.assertEqual(fake.updates(),[])

    def test_symlink_skill_is_rejected_before_native_update(self):
        path=self.github()
        file=path/"SKILL.md"
        file.rename(path/"original")
        file.symlink_to(path/"original")
        fake=FakeGH(self.rows)
        with self.assertRaisesRegex(RuntimeError,"non-symlink"):
            self.run_helper(fake)
        self.assertEqual(fake.updates(),[])

    def test_unknown_corrupt_or_pinned_lock_stops_before_native_update(self):
        self.github()
        self.npx("unrelated")
        path=self.write_lock()
        baseline=json.loads(path.read_text())
        for mode in ("unknown-top","unknown-entry","null-entry","bool-version","invalid-dismissed","pinned","foreign","corrupt"):
            value=json.loads(json.dumps(baseline))
            if mode=="unknown-top":
                value["unknown"]="PRIVATE_LOCK_SENTINEL"
            elif mode=="unknown-entry":
                value["skills"]["unrelated"]["unknown"]="PRIVATE_LOCK_SENTINEL"
            elif mode=="null-entry":
                value["skills"]["unrelated"]["installedAt"]=None
            elif mode=="bool-version":
                value["version"]=True
            elif mode=="invalid-dismissed":
                value["dismissed"]={"choice":0}
            elif mode in ("pinned","foreign"):
                record=dict(value["skills"]["unrelated"])
                record["skillPath"]="skills/managed/SKILL.md"
                if mode=="pinned":
                    record["pinnedRef"]="v1"
                else:
                    record["source"]="different/publisher"
                    record["sourceUrl"]="https://github.com/different/publisher"
                value["skills"]["namespace/managed"]=record
            path.write_text("{bad" if mode=="corrupt" else json.dumps(value))
            before=path.read_bytes()
            fake=FakeGH(self.rows)
            with self.subTest(mode=mode),self.assertRaises(RuntimeError) as error:
                self.run_helper(fake)
            self.assertEqual(fake.updates(),[])
            self.assertEqual(path.read_bytes(),before)
            self.assertNotIn("PRIVATE_LOCK_SENTINEL",str(error.exception))

    def test_native_lock_clobber_and_dry_run_mutation_fail_with_backup_guidance(self):
        self.github()
        self.npx("unrelated")
        self.write_lock()
        fake=FakeGH(self.rows)
        fake.clobber_lock=True
        with self.assertRaisesRegex(RuntimeError,"backup"):
            self.run_helper(fake)
        self.write_lock()
        fake=FakeGH(self.rows)
        fake.mutate_dry_lock=True
        with self.assertRaisesRegex(RuntimeError,"backup"):
            self.run_helper(fake,True)

    def test_planned_update_requires_a_matching_native_shared_lock_record(self):
        self.github()
        fake=FakeGH(self.rows)
        fake.record_write=False
        with self.assertRaisesRegex(RuntimeError,"did not record.*backup"):
            self.run_helper(fake)

if __name__=="__main__":
    unittest.main()
