"""A Hugging Face Hub in memory, for the publishing tests: records uploads and tags."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeHub:
    existing_tags: set[str] = field(default_factory=set)
    fail_on_tag: bool = False
    uploads: list[set[str]] = field(default_factory=list)
    tagged: list[tuple[str, str]] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)

    def create_model_repo(self, repo_id: str) -> None:
        self.repos.append(repo_id)

    def tags(self, repo_id: str) -> frozenset[str]:
        return frozenset(self.existing_tags)

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        self.uploads.append({path.name for path in folder.iterdir()})
        return "b" * 40

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None:
        if self.fail_on_tag:
            self.fail_on_tag = False
            raise ConnectionError("dropped")
        self.existing_tags.add(tag)
        self.tagged.append((tag, revision))
