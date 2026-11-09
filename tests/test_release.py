"""Tests for the release.py script."""

import os
import subprocess
import textwrap
import zipfile
from pathlib import Path
from xml.etree.ElementTree import parse

import pytest

from release import (
    ReleaseScriptError,
    commit_release_changes,
    get_plugin_metadata,
    main,
    package_plugin,
    run_release_process,
    update_repository_file,
)


@pytest.fixture
def temp_plugin_dir(tmp_path: Path) -> Path:
    """Create a temporary plugin directory structure for testing.

    Args:
        tmp_path: The pytest temporary path fixture.

    Returns:
        The path to the created temporary plugin directory.
    """
    plugin_dir: Path = tmp_path / "my_plugin"
    plugin_dir.mkdir()

    # Create dummy files and dirs to be packaged, based on metadata.txt
    (plugin_dir / "__init__.py").touch()
    (plugin_dir / "My_Test_Plugin.py").touch()
    (plugin_dir / "modules").mkdir()
    (plugin_dir / "modules" / "some_module.py").touch()
    (plugin_dir / "resources").mkdir()
    (plugin_dir / "resources" / "styles.qss").touch()
    (plugin_dir / "resources" / "icons").mkdir()
    (plugin_dir / "resources" / "icons" / "main_icon.svg").touch()
    (plugin_dir / "i18n").mkdir()
    (plugin_dir / "i18n" / "de.ts").touch()

    # Create files/dirs that should be excluded
    (plugin_dir / "dev_only").mkdir()
    (plugin_dir / "dev_only" / "notes.txt").touch()
    (plugin_dir / "some_file.pyc").touch()
    (plugin_dir / "__pycache__").mkdir()
    (plugin_dir / "__pycache__" / "cache.pyc").touch()

    return plugin_dir


@pytest.fixture
def release_repo_dir(tmp_path: Path) -> Path:
    """Create a temporary release repository directory.

    Args:
        tmp_path: The pytest temporary path fixture.

    Returns:
        The path to the created temporary release repository directory.
    """
    repo_dir: Path = tmp_path / "release_repo" / "docs"
    repo_dir.mkdir(parents=True)
    return repo_dir


@pytest.fixture
def metadata_path(temp_plugin_dir: Path) -> Path:
    """Create a metadata.txt file and return its path.

    Args:
        temp_plugin_dir: The temporary plugin directory.

    Returns:
        The path to the created metadata.txt file.
    """
    metadata_content: str = textwrap.dedent("""
        [general]
        name = My Test Plugin
        version = 1.0.0
        changelog = 
            Version 1.0.0: 
            - Initial release.
            
            Version 0.9:
            - Fixed a bug.
            
        description = A test plugin.
        qgisMinimumVersion = 3.40
        qgisMaximumVersion = 4.99
        author = Test Author
        email = test@example.com
        icon=resources/icons/main_icon.svg
        download_url_base=https://example.github.io/QGIS-Plugin-Releases


        [release]
        # The name of the root directory inside the .zip file.
        # This should match the plugin's package name.
        plugin_package_name = My_Test_Plugin

        # Files and directories to include in the plugin package.
        files_to_package =
            __init__.py
            My_Test_Plugin.py
            metadata.txt
        dirs_to_package =
            modules
            i18n
            resources
        translation_dir = i18n
        excluded_dirs = __pycache__
        excluded_extensions = .pyc
        """)
    meta_file: Path = temp_plugin_dir / "metadata.txt"
    meta_file.write_text(metadata_content, encoding="utf-8")
    return meta_file


@pytest.fixture(autouse=True)
def change_test_dir(temp_plugin_dir: Path) -> None:
    """Change CWD to the temporary plugin directory for test execution."""
    original_dir: Path = Path.cwd()
    os.chdir(temp_plugin_dir)
    yield
    os.chdir(original_dir)


def test_get_plugin_metadata_success(metadata_path: Path) -> None:
    """Test successful reading of metadata.txt."""
    metadata = get_plugin_metadata()
    assert metadata["name"] == "My Test Plugin"
    assert metadata["version"] == "1.0.0"
    assert metadata["plugin_package_name"] == "My_Test_Plugin"
    assert "resources" in metadata["dirs_to_package"]


def test_get_plugin_metadata_file_not_found() -> None:
    """Test error when metadata.txt is not found."""
    with pytest.raises(ReleaseScriptError, match="Metadata file not found"):
        get_plugin_metadata()


def test_update_repository_create_new_xml(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Test creating a new plugins.xml file."""
    metadata = get_plugin_metadata()
    update_repository_file(metadata, release_repo_dir)

    xml_path: Path = release_repo_dir / "plugins.xml"
    assert xml_path.exists()

    tree = parse(xml_path)
    root = tree.getroot()
    assert root.tag == "plugins"
    plugin_node = root.find("pyqgis_plugin[@name='My Test Plugin']")
    assert plugin_node is not None
    assert plugin_node.get("version") == "1.0.0"
    assert plugin_node.find("version").text == "1.0.0"
    assert plugin_node.find("author_name").text == "Test Author"
    assert plugin_node.find("file_name").text == "My_Test_Plugin.zip"


def test_update_repository_update_existing_plugin(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Test updating an existing plugin entry in plugins.xml."""
    # 1. Create initial plugins.xml with version 1.0.0
    metadata_v1 = get_plugin_metadata()
    update_repository_file(metadata_v1, release_repo_dir)

    # 2. Update metadata.txt to version 1.1.0
    with metadata_path.open("r+", encoding="utf-8") as f:
        content = f.read()
        content = content.replace("version = 1.0.0", "version = 1.1.0")
        f.seek(0)
        f.write(content)
        f.truncate()

    # 3. Run update again with new metadata
    metadata_v2 = get_plugin_metadata()
    assert metadata_v2["version"] == "1.1.0"
    update_repository_file(metadata_v2, release_repo_dir)

    # 4. Verify the update
    xml_path: Path = release_repo_dir / "plugins.xml"
    tree = parse(xml_path)
    plugin_node = tree.getroot().find("pyqgis_plugin[@name='My Test Plugin']")
    assert plugin_node is not None
    assert plugin_node.get("version") == "1.1.0"
    assert plugin_node.find("version").text == "1.1.0"


def test_update_repository_adds_second_plugin(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Test adding a second plugin to an existing plugins.xml.

    This test ensures that when a new plugin is added to an existing
    plugins.xml, the entries for other plugins remain intact and are not
    corrupted. It specifically guards against issues like missing closing tags
    for previous entries, which would make the XML malformed.
    """
    # 1. Create metadata for a different plugin and add it first.
    # We copy the existing metadata, then modify key fields for a distinct entry.
    metadata_first = get_plugin_metadata()
    metadata_first["name"] = "First Existing Plugin"
    metadata_first["version"] = "0.5.0"
    metadata_first["description"] = "Description for first plugin"

    update_repository_file(metadata_first, release_repo_dir)

    # 2. Add the second plugin (the one actually defined in metadata.txt)
    metadata_second = get_plugin_metadata()
    update_repository_file(metadata_second, release_repo_dir)

    # 3. Verify both plugins are now in the XML and the first is intact.
    # The ability to parse the file is the first check for well-formedness.
    xml_path = release_repo_dir / "plugins.xml"
    tree = parse(xml_path)
    root = tree.getroot()
    plugins = root.findall("pyqgis_plugin")
    assert len(plugins) == 2, "Expected to find two plugin entries in the XML."

    # 4. Verify the integrity of the first plugin's entry
    first_plugin_node = root.find("pyqgis_plugin[@name='First Existing Plugin']")
    assert first_plugin_node is not None, "First plugin entry not found."
    assert first_plugin_node.get("version") == "0.5.0", (
        "Version attribute of first plugin is incorrect."
    )
    assert (
        first_plugin_node.find("description").text == "Description for first plugin"
    ), "Description of first plugin is incorrect."

    # 5. Verify the presence of the second plugin
    second_plugin_node = root.find("pyqgis_plugin[@name='My Test Plugin']")
    assert second_plugin_node is not None, "Second plugin entry not found."


def test_plugins_xml_structure_completeness(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Test that the generated XML entry contains all required QGIS repository tags.

    This ensures that tags like <qgis_minimum_version>, <qgis_maximum_version>,
    <description>, etc., are correctly populated in the plugins.xml file.
    """
    metadata = get_plugin_metadata()
    update_repository_file(metadata, release_repo_dir)

    xml_path: Path = release_repo_dir / "plugins.xml"
    tree = parse(xml_path)
    plugin_node = tree.getroot().find("pyqgis_plugin")

    assert plugin_node is not None
    # 1. Validating attributes
    assert plugin_node.tag == "pyqgis_plugin"
    assert plugin_node.get("name") == "My Test Plugin"
    assert plugin_node.get("version") == "1.0.0"

    # 2. Validating required child tags
    expected_tags: dict[str, str] = {
        "version": "1.0.0",
        "description": "A test plugin.",
        "qgis_minimum_version": "3.40",
        "qgis_maximum_version": "4.99",
        "author_name": "Test Author",
        "email": "test@example.com",
        "file_name": "My_Test_Plugin.zip",
    }

    for tag, content in expected_tags.items():
        element = plugin_node.find(tag)
        assert element is not None, f"Missing required tag: <{tag}>"
        assert element.text == content, f"Incorrect content for tag: <{tag}>"

    # 3. Check tags with variable content separately
    changelog = plugin_node.find("changelog")
    assert changelog is not None
    assert "Version 1.0.0" in changelog.text

    download_url = plugin_node.find("download_url")
    assert download_url is not None
    assert (
        download_url.text
        == "https://example.github.io/QGIS-Plugin-Releases/My_Test_Plugin.zip"
    )
    assert str(release_repo_dir) not in download_url.text


def test_package_plugin_creates_zip_correctly(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Test that package_plugin creates a zip file with correct contents."""
    metadata = get_plugin_metadata()
    package_plugin(metadata, release_repo_dir)

    zip_path: Path = release_repo_dir / "My_Test_Plugin.zip"
    assert zip_path.exists()

    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()

        # Check for files that should be included based on metadata.txt
        assert "My_Test_Plugin/metadata.txt" in namelist
        assert "My_Test_Plugin/__init__.py" in namelist
        assert "My_Test_Plugin/My_Test_Plugin.py" in namelist
        assert "My_Test_Plugin/modules/some_module.py" in namelist
        assert "My_Test_Plugin/i18n/de.ts" in namelist
        assert "My_Test_Plugin/resources/styles.qss" in namelist
        assert "My_Test_Plugin/resources/icons/main_icon.svg" in namelist

        # Check that excluded files/dirs are not present
        assert not any("dev_only" in name for name in namelist)
        assert not any(name.endswith(".pyc") for name in namelist)
        assert not any("__pycache__" in name for name in namelist)

        # Check against double-nesting
        assert "My_Test_Plugin/My_Test_Plugin/__init__.py" not in namelist

        # Ensure no other files were accidentally included
        assert len(namelist) == 7


def test_run_release_process_removes_dev_marker(
    release_repo_dir: Path, metadata_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that the full release process removes the (dev) marker."""
    with metadata_path.open("r+", encoding="utf-8") as f:
        content = f.read()
        content = content.replace(
            "name = My Test Plugin", "name = My Test Plugin (dev)"
        )
        f.seek(0)
        f.write(content)
        f.truncate()

    monkeypatch.setattr("release.compile_translations", lambda _metadata: None)

    run_release_process(release_repo_dir)

    # Check plugins.xml for clean name
    xml_path: Path = release_repo_dir / "plugins.xml"
    tree = parse(xml_path)
    plugin_node = tree.find("pyqgis_plugin[@name='My Test Plugin']")
    assert plugin_node is not None
    assert tree.find("pyqgis_plugin[@name='My Test Plugin (dev)']") is None

    # Check metadata.txt inside zip for clean name
    zip_path: Path = release_repo_dir / "My_Test_Plugin.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open("My_Test_Plugin/metadata.txt") as meta_in_zip:
            content = meta_in_zip.read().decode("utf-8")
            assert "name = My Test Plugin" in content
            assert "(dev)" not in content


def test_commit_release_changes_commits_only_release_files(
    release_repo_dir: Path, metadata_path: Path
) -> None:
    """Commit release outputs without including unrelated staged changes."""
    repo_dir = release_repo_dir.parent
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.com"],
        cwd=repo_dir,
        check=True,
    )

    unrelated_file = repo_dir / "unrelated.txt"
    unrelated_file.write_text("unrelated", encoding="utf-8")
    (release_repo_dir / "My_Test_Plugin.zip").write_bytes(b"plugin")
    (release_repo_dir / "plugins.xml").write_text("<plugins />", encoding="utf-8")
    subprocess.run(
        ["git", "add", "unrelated.txt"],
        cwd=repo_dir,
        check=True,
    )

    commit_release_changes(get_plugin_metadata(), release_repo_dir)

    committed_files = subprocess.run(
        ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(committed_files) == {
        "docs/My_Test_Plugin.zip",
        "docs/plugins.xml",
    }
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines() == ["unrelated.txt"]


def test_commit_release_changes_requires_git_repository(
    tmp_path: Path, metadata_path: Path
) -> None:
    """Fail clearly when the output directory is not in a Git repository."""
    output_dir = tmp_path / "not_a_repo"
    output_dir.mkdir()

    with pytest.raises(ReleaseScriptError, match="not inside a Git repository"):
        commit_release_changes(get_plugin_metadata(), output_dir)


def test_commit_release_changes_pushes_to_configured_upstream(
    tmp_path: Path, release_repo_dir: Path, metadata_path: Path
) -> None:
    """Commit generated files and push the release branch to its upstream."""
    repo_dir = release_repo_dir.parent
    remote_dir = tmp_path / "release_remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote_dir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.com"],
        cwd=repo_dir,
        check=True,
    )
    (repo_dir / "README.txt").write_text("release repository", encoding="utf-8")
    subprocess.run(["git", "add", "README.txt"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_dir)],
        cwd=repo_dir,
        check=True,
    )
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", "main"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    (release_repo_dir / "My_Test_Plugin.zip").write_bytes(b"plugin")
    (release_repo_dir / "plugins.xml").write_text("<plugins />", encoding="utf-8")
    commit_release_changes(get_plugin_metadata(), release_repo_dir, push=True)

    remote_files = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote_dir),
            "show",
            "main:docs/plugins.xml",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remote_files.stdout == "<plugins />"


@pytest.mark.parametrize(
    ("menu_input", "expected_output_dir", "expected_commit", "expected_push"),
    [
        (["1"], Path(r"C:\Github_Repos\QGIS-Plugin-Releases\docs"), False, False),
        (["2", r"C:\custom release docs"], Path(r"C:\custom release docs"), False, False),
        (["3"], Path(r"C:\Github_Repos\QGIS-Plugin-Releases\docs"), True, False),
        (["4"], Path(r"C:\Github_Repos\QGIS-Plugin-Releases\docs"), False, True),
    ],
)
def test_main_menu_selects_release_options(
    monkeypatch: pytest.MonkeyPatch,
    menu_input: list[str],
    expected_output_dir: Path,
    expected_commit: bool,
    expected_push: bool,
) -> None:
    """Map interactive menu choices to the corresponding release settings."""
    received: dict[str, object] = {}

    def fake_run_release_process(
        output_dir: Path, *, commit: bool = False, push: bool = False
    ) -> None:
        received["output_dir"] = output_dir
        received["commit"] = commit
        received["push"] = push

    monkeypatch.setattr("release.run_release_process", fake_run_release_process)
    monkeypatch.setattr("sys.argv", ["release.py"])
    inputs = iter(menu_input)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert main() == 0
    assert received == {
        "output_dir": expected_output_dir,
        "commit": expected_commit,
        "push": expected_push,
    }


def test_main_menu_quit_does_not_run_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quit the menu without starting a release."""
    monkeypatch.setattr("release.run_release_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("sys.argv", ["release.py"])
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")

    assert main() == 0


def test_package_plugin_name_mismatch(
    metadata_path: Path, release_repo_dir: Path
) -> None:
    """Test that packaging fails if plugin_package_name is incorrect."""
    with metadata_path.open("r+", encoding="utf-8") as f:
        content = f.read()
        content = content.replace(
            "plugin_package_name = My_Test_Plugin",
            "plugin_package_name = wrong_name",
        )
        f.seek(0)
        f.write(content)
        f.truncate()

    metadata = get_plugin_metadata()
    with pytest.raises(ReleaseScriptError, match="Name mismatch"):
        package_plugin(metadata, release_repo_dir)
