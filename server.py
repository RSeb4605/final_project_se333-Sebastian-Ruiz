from fastmcp import FastMCP
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any
import fnmatch
import json

mcp = FastMCP("Testing Agent 🚀")


def _compute_jacoco_coverage(jacoco_xml_path: str) -> Dict[str, Any]:
    """Return overall line coverage percent from a JaCoCo XML report.

    Returns {percent: float, covered: int, missed: int}
    """
    p = Path(jacoco_xml_path)
    if not p.exists():
        return {"ok": False, "msg": f"File not found: {jacoco_xml_path}"}
    try:
        tree = ET.parse(p)
        root = tree.getroot()
        total_missed = 0
        total_covered = 0
        for counter in root.findall('.//counter'):
            if counter.get('type') == 'LINE':
                total_missed += int(counter.get('missed'))
                total_covered += int(counter.get('covered'))
        total = total_missed + total_covered
        percent = (total_covered / total * 100.0) if total > 0 else 0.0
        return {"ok": True, "percent": percent, "covered": total_covered, "missed": total_missed}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@mcp.tool
def git_status(repo_dir: str = ".") -> Dict[str, Any]:
    """Return git status summary: clean, staged files, unstaged files, conflicts, branch."""
    repo_dir = os.path.abspath(repo_dir)
    res = _run_cmd(["git", "status", "--porcelain", "--branch"], cwd=repo_dir)
    out = res.get("stdout", "")
    lines = [l for l in out.splitlines() if l.strip()]
    branch = None
    staged = []
    unstaged = []
    conflicts = []
    for ln in lines:
        if ln.startswith("##"):
            branch = ln[2:].strip()
            continue
        # porcelain: XY <path>
        status = ln[:2]
        path = ln[3:]
        x, y = status[0], status[1]
        if x == 'U' or y == 'U' or (x == 'A' and y == 'A'):
            conflicts.append(path)
        if x != ' ':
            staged.append(path)
        if y != ' ':
            unstaged.append(path)

    clean = (len(staged) == 0 and len(unstaged) == 0 and len(conflicts) == 0)
    return {"ok": True, "clean": clean, "branch": branch, "staged": staged, "unstaged": unstaged, "conflicts": conflicts, "raw": out}


@mcp.tool
def git_add_all(repo_dir: str = ".", exclude_patterns: List[str] = None) -> Dict[str, Any]:
    """Stage all changes intelligently, excluding common build artifacts.

    Returns list of staged files and counts. Uses `exclude_patterns` on file paths.
    """
    repo_dir = os.path.abspath(repo_dir)
    if exclude_patterns is None:
        exclude_patterns = [
            "target/**", "**/target/**", "**/*.class", "**/*.jar", "*.log", "**/.idea/**", "**/.vscode/**", "**/*.pyc", "node_modules/**"
        ]

    st = _run_cmd(["git", "status", "--porcelain"], cwd=repo_dir)
    files = []
    for ln in (st.get("stdout", "") or "").splitlines():
        if not ln.strip():
            continue
        path = ln[3:]
        skip = False
        for pat in exclude_patterns:
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.join(repo_dir, path), pat):
                skip = True
                break
        if not skip:
            files.append(path)

    if not files:
        return {"ok": True, "staged": [], "msg": "No files to stage after filtering."}

    # Stage files
    add_cmd = ["git", "add", "--"] + files
    add_res = _run_cmd(add_cmd, cwd=repo_dir)

    # Confirm staged
    staged_res = _run_cmd(["git", "diff", "--name-only", "--cached"], cwd=repo_dir)
    staged_files = [l for l in (staged_res.get("stdout", "") or "").splitlines() if l.strip()]

    return {"ok": add_res.get("returncode") == 0, "staged": staged_files, "add_stdout": add_res.get("stdout"), "add_stderr": add_res.get("stderr")}


@mcp.tool
def git_commit(message: str, repo_dir: str = ".", include_coverage: bool = False, jacoco_xml: str = None, coverage_threshold: float = None) -> Dict[str, Any]:
    """Commit staged changes with a standardized message. Optionally append coverage stats.

    - If `include_coverage` and `jacoco_xml` provided, compute coverage and append to message.
    - If `coverage_threshold` provided and coverage < threshold, the commit will be aborted and returned as not-ok.
    """
    repo_dir = os.path.abspath(repo_dir)

    # Check staged
    staged_res = _run_cmd(["git", "diff", "--name-only", "--cached"], cwd=repo_dir)
    staged = [l for l in (staged_res.get("stdout", "") or "").splitlines() if l.strip()]
    if not staged:
        return {"ok": False, "msg": "No staged changes to commit."}

    final_message = message
    coverage_info = None
    if include_coverage and jacoco_xml:
        cov = _compute_jacoco_coverage(jacoco_xml)
        if cov.get("ok"):
            coverage_info = cov
            percent = cov.get("percent", 0.0)
            final_message = f"{message} | Coverage: {percent:.2f}% ({cov.get('covered')}/{cov.get('covered')+cov.get('missed')})"
            if coverage_threshold is not None and percent < coverage_threshold:
                return {"ok": False, "msg": f"Coverage {percent:.2f}% below threshold {coverage_threshold}% - aborting commit.", "coverage": cov}
        else:
            final_message = f"{message} | Coverage: unknown ({cov.get('msg')})"

            final_message = f"{message} | Coverage: {percent:.2f}% ({cov.get('covered')}/{cov.get('total')})"
    std_msg = final_message

    commit_res = _run_cmd(["git", "commit", "-m", std_msg], cwd=repo_dir)
    return {"ok": commit_res.get("returncode") == 0, "stdout": commit_res.get("stdout"), "stderr": commit_res.get("stderr"), "coverage": coverage_info}


@mcp.tool
def git_push(remote: str = "origin", repo_dir: str = ".") -> Dict[str, Any]:
    """Push current branch to remote. Sets upstream if not present.

    Returns push output and remote URL if available.
    """
    repo_dir = os.path.abspath(repo_dir)
    # determine current branch
    br = _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    branch = (br.get("stdout") or "").strip()
    if not branch:
        return {"ok": False, "msg": "Cannot determine current branch."}

    # Try normal push
    push_res = _run_cmd(["git", "push", remote, branch], cwd=repo_dir)
    if push_res.get("returncode") == 0:
        return {"ok": True, "stdout": push_res.get("stdout"), "stderr": push_res.get("stderr"), "branch": branch}

    # If failed, attempt to set upstream
    push_up_res = _run_cmd(["git", "push", "-u", remote, branch], cwd=repo_dir)
    return {"ok": push_up_res.get("returncode") == 0, "stdout": push_up_res.get("stdout"), "stderr": push_up_res.get("stderr"), "branch": branch}


@mcp.tool
def git_pull_request(base: str = "main", title: str = None, body: str = None, repo_dir: str = ".") -> Dict[str, Any]:
    """Create a pull request against `base` branch. Prefers `gh` CLI if available.

    Returns PR URL on success. If `gh` is not available, returns guidance.
    """
    repo_dir = os.path.abspath(repo_dir)
    # Determine title/body defaults
    if not title:
        title = "[AUTO] Pull request: changes and tests"
    if not body:
        body = "Automated PR created by MCP tools. Includes test or coverage updates when applicable.\n\nMetadata:\n"

    # Try GitHub CLI first
    gh_check = _run_cmd(["gh", "--version"], cwd=repo_dir)
    if gh_check.get("returncode") == 0:
        cmd = ["gh", "pr", "create", "--base", base, "--title", title, "--body", body]
        pr_res = _run_cmd(cmd, cwd=repo_dir)
        if pr_res.get("returncode") == 0:
            # gh prints URL to stdout
            url = (pr_res.get("stdout") or "").strip().splitlines()[-1] if pr_res.get("stdout") else None
            return {"ok": True, "url": url, "stdout": pr_res.get("stdout")}
        else:
            return {"ok": False, "msg": "gh CLI failed to create PR", "stderr": pr_res.get("stderr"), "stdout": pr_res.get("stdout")}

    # If gh not available, provide guidance for using GitHub API or web
    return {"ok": False, "msg": "GitHub CLI 'gh' not found. Install 'gh' or provide a GitHub token to use the API. Alternatively, create a PR manually via the host (GitHub/GitLab)."}


@mcp.tool
def maven_run(project_dir: str = "codebase", goals: List[str] = None) -> Dict[str, Any]:
    """Run Maven in the given project directory with specified goals.

    Example: `maven_run('codebase', ['clean', 'test'])`
    """
    if goals is None:
        goals = ["test"]
    cmd = ["mvn"] + goals
    project_dir = os.path.abspath(project_dir)
    return _run_cmd(cmd, cwd=project_dir)


@mcp.tool
def configure_jacoco(project_dir: str = "codebase") -> Dict[str, Any]:
    """Ensure a basic JaCoCo plugin snippet exists in `pom.xml`.

    This will back up the original `pom.xml` to `pom.xml.bak` and insert a minimal
    `jacoco-maven-plugin` configuration under `<build><plugins>` if not already present.
    """
    p = Path(project_dir)
    pom = p / "pom.xml"
    if not pom.exists():
        return {"ok": False, "msg": f"pom.xml not found in {project_dir}"}

    text = pom.read_text(encoding="utf-8")
    if "jacoco-maven-plugin" in text:
        return {"ok": True, "msg": "JaCoCo already configured in pom.xml"}

    # Backup
    backup = pom.with_suffix('.xml.bak')
    if not backup.exists():
        pom.replace(backup)
        backup.write_text(text, encoding="utf-8")

    # Minimal plugin snippet
    plugin_snippet = """
            <plugin>
              <groupId>org.jacoco</groupId>
              <artifactId>jacoco-maven-plugin</artifactId>
              <version>0.8.10</version>
              <executions>
                <execution>
                  <goals>
                    <goal>prepare-agent</goal>
                  </goals>
                </execution>
                <execution>
                  <id>report</id>
                  <phase>test</phase>
                  <goals>
                    <goal>report</goal>
                  </goals>
                </execution>
              </executions>
            </plugin>
    """

    # Try to insert under <build><plugins>
    if "<build>" in text:
        if "<plugins>" in text:
            text = text.replace("<plugins>", "<plugins>\n" + plugin_snippet, 1)
        else:
            text = text.replace("<build>", "<build>\n  <plugins>\n" + plugin_snippet + "\n  </plugins>")
    else:
        # Add a build section near the end
        text = text.replace("</project>", "  <build>\n    <plugins>\n" + plugin_snippet + "\n    </plugins>\n  </build>\n</project>")

    pom.write_text(text, encoding="utf-8")
    return {"ok": True, "msg": "Inserted JaCoCo plugin into pom.xml (backup created)"}


@mcp.tool
def generate_tests(project_dir: str = "codebase", out_dir: str = None) -> Dict[str, Any]:
    """Generate simple JUnit test skeletons for public methods found in Java sources.

    - Scans `src/main/java` under `project_dir`.
    - Writes test classes to `src/test/java` mirroring packages.
    """
    p = Path(project_dir)
    src_root = p / "src" / "main" / "java"
    if out_dir:
        test_root = Path(out_dir)
    else:
        test_root = p / "src" / "test" / "java"

    if not src_root.exists():
        return {"ok": False, "msg": f"Source root not found: {src_root}"}

    created = 0
    method_rx = re.compile(r"public\s+(?:static\s+)?[\w<>\[\]]+\s+(\w+)\s*\(([^)]*)\)")
    package_rx = re.compile(r"package\s+([\w\.]+);")

    for java_file in src_root.rglob("*.java"):
        text = java_file.read_text(encoding="utf-8")
        pkg_match = package_rx.search(text)
        package = pkg_match.group(1) if pkg_match else ""
        methods = method_rx.findall(text)
        if not methods:
            continue

        class_name = java_file.stem
        test_class_name = class_name + "Test"
        # Determine output directory for package
        pkg_path = package.replace('.', os.sep) if package else ""
        out_dir_full = test_root / pkg_path
        out_dir_full.mkdir(parents=True, exist_ok=True)
        test_file = out_dir_full / (test_class_name + ".java")
        if test_file.exists():
            # skip existing tests to avoid overwriting
            continue

        imports = """import org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.*;\n"""

        body_lines = [f"package {package};\n" if package else ""]
        body_lines.append(imports)
        body_lines.append(f"public class {test_class_name} {{\n")
        for mname, margs in methods:
            test_method = f"    @Test\n    public void test_{mname}() {{\n        // TODO: add assertions for {mname}\n        fail(\"Not yet implemented\");\n    }}\n\n"
            body_lines.append(test_method)

        body_lines.append("}\n")
        test_file.write_text(''.join(body_lines), encoding="utf-8")
        created += 1

    return {"ok": True, "created_tests": created}


@mcp.tool
def analyze_coverage(project_dir: str = "codebase", jacoco_xml: str = None) -> Dict[str, Any]:
    """Parse a JaCoCo XML report and return uncovered classes/methods and recommendations.

    If `jacoco_xml` is None, search common locations under `project_dir/target`.
    """
    p = Path(project_dir)
    candidates = []
    if jacoco_xml:
        candidates = [Path(jacoco_xml)]
    else:
        candidates = list(p.rglob("jacoco.xml")) + list(p.rglob("**/jacoco.xml"))

    if not candidates:
        # common site location
        candidates = [p / "target" / "site" / "jacoco" / "jacoco.xml"]

    found = None
    for c in candidates:
        if c.exists():
            found = c
            break

    if not found:
        return {"ok": False, "msg": "JaCoCo XML report not found. Run Maven tests with JaCoCo enabled."}

    tree = ET.parse(found)
    root = tree.getroot()
    uncovered = []
    # JaCoCo XML: packages -> package -> class -> counter type="LINE"
    for pkg in root.findall('.//package'):
        pkg_name = pkg.get('name')
        for cls in pkg.findall('class'):
            cls_name = cls.get('name')
            line_counter = None
            for counter in cls.findall('counter'):
                if counter.get('type') == 'LINE':
                    missed = int(counter.get('missed'))
                    covered = int(counter.get('covered'))
                    if missed > 0:
                        uncovered.append({
                            'package': pkg_name,
                            'class': cls_name,
                            'missed': missed,
                            'covered': covered
                        })

    recommendations = []
    for item in uncovered:
        recommendations.append(f"Increase tests for {item['package']}.{item['class']} (missed lines: {item['missed']})")

    return {"ok": True, "report": {"file": str(found), "uncovered": uncovered, "recommendations": recommendations}}


def _parse_surefire_reports(project_dir: str = "codebase") -> Dict[str, Any]:
    """Parse Maven Surefire reports for test failures and stack traces.

    Returns a dict with keys: failures (list of dicts with classname, name, message, stacktrace)
    """
    p = Path(project_dir)
    reports_dir = p / "target" / "surefire-reports"
    results = {"failures": []}
    if not reports_dir.exists():
        return results

    for xml_file in reports_dir.glob("*.xml"):
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            for tc in root.findall('.//testcase'):
                failure = tc.find('failure') or tc.find('error')
                if failure is not None:
                    classname = tc.get('classname')
                    name = tc.get('name')
                    message = failure.get('message') if failure.get('message') else ''
                    stack = failure.text or ''
                    results['failures'].append({'classname': classname, 'name': name, 'message': message, 'stacktrace': stack})
        except Exception:
            # ignore malformed files
            continue

    return results


def _write_fix_proposal(failures: List[Dict[str, Any]], project_dir: str, iteration: int) -> str:
    """Write a fix proposal file (json + placeholder patch) and return path to proposal."""
    base = Path(project_dir) / '.mcp' / 'fixes'
    base.mkdir(parents=True, exist_ok=True)
    meta_path = base / f'proposal_{iteration}.json'
    patch_path = base / f'proposal_{iteration}.patch'
    meta = {'iteration': iteration, 'failures': failures}
    meta_path.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    # Create a placeholder patch explaining the proposed fix steps
    patch_text = """# Proposal patch placeholder\n# This file contains suggested changes to fix failing tests found during iteration.\n# Inspect the failures in the .json file and edit this patch to include actual unified-diff content.\n"""
    patch_path.write_text(patch_text, encoding='utf-8')
    return str(meta_path)


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers (simple demo tool)"""
    return a + b


if __name__ == "__main__":
    # Allow running the MCP server normally (e.g., `mcp.run()`) or invoking a tool via args.
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "run_tool":
        # Example: python server.py run_tool generate_tests codebase
        tool_name = sys.argv[2]
        args = sys.argv[3:]
        # Very small dispatcher for convenience
        if tool_name == 'generate_tests':
            res = generate_tests(*args) if args else generate_tests()
            print(res)
        elif tool_name == 'configure_jacoco':
            res = configure_jacoco(*args) if args else configure_jacoco()
            print(res)
        elif tool_name == 'maven_run':
            # pass goals as separate args
            project = args[0] if args else 'codebase'
            goals = args[1:] if len(args) > 1 else ['test']
            res = maven_run(project, goals)
            print(res)
        elif tool_name == 'analyze_coverage':
            project = args[0] if args else 'codebase'
            res = analyze_coverage(project)
            print(res)
        else:
            print({"ok": False, "msg": f"Unknown tool {tool_name}"})
    else:
        mcp.run(transport="sse")