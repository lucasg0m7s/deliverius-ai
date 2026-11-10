"""Publishing a model folder to the Hugging Face Hub: a dry run by default, tagged, resumable.

`publish` stages the folder, shows what would be sent and stops there unless `confirm` is set.
Then it creates the repository, refuses a tag that already exists before any upload, uploads the
folder in one commit and tags it. A run that died between the upload and the tag is simply run
again: the tag is still free, so the folder goes up once more and gets tagged. Shared by the
fraud and the intent research; what the author reads is Portuguese.
"""

import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from huggingface_hub import HfApi
from pydantic import SecretStr

from deliverius.config import Settings
from deliverius.shared.errors import DeliveriusError


class PublishError(DeliveriusError):
    """Publishing cannot go on; `rule` is a stable code and `detail` never holds a secret."""

    def __init__(self, rule: str, detail: str | None = None) -> None:
        super().__init__(rule if detail is None else f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


class ModelHub(Protocol):
    """What publishing asks of the Hub; a test replaces it."""

    def create_model_repo(self, repo_id: str) -> None: ...

    def tags(self, repo_id: str) -> frozenset[str]: ...

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        """Uploads every file of `folder` in one commit and returns the commit hash."""
        ...

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None: ...


class HuggingFaceModelHub:
    def __init__(self, api: HfApi) -> None:
        self._api = api

    def create_model_repo(self, repo_id: str) -> None:
        self._api.create_repo(repo_id=repo_id, repo_type="model", private=False, exist_ok=True)

    def tags(self, repo_id: str) -> frozenset[str]:
        refs = self._api.list_repo_refs(repo_id, repo_type="model")
        return frozenset(ref.name for ref in refs.tags)

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> str:
        commit = self._api.upload_folder(
            repo_id=repo_id, repo_type="model", folder_path=folder, commit_message=message
        )
        return str(commit.oid)

    def create_tag(self, repo_id: str, tag: str, revision: str) -> None:
        self._api.create_tag(repo_id, tag=tag, revision=revision, repo_type="model")


type HubFactory = Callable[[SecretStr], ModelHub]


def huggingface_model_hub(token: SecretStr) -> ModelHub:
    return HuggingFaceModelHub(HfApi(token=token.get_secret_value()))


@dataclass(frozen=True, slots=True)
class Upload:
    repo_id: str
    tag: str
    folder: Path
    message: str


@dataclass(frozen=True, slots=True)
class Staged:
    """A staged folder: what to send, and the lines that tell the author what it holds."""

    upload: Upload
    lines: tuple[str, ...]


type Stager = Callable[[Path], Staged]


def send(upload: Upload, hub: ModelHub) -> str:
    """Upload and tag; returns the commit to pin. Never overwrites a version."""
    hub.create_model_repo(upload.repo_id)
    if upload.tag in hub.tags(upload.repo_id):
        raise PublishError("publish.tag_exists", upload.tag)
    revision = hub.upload_folder(upload.repo_id, upload.folder, upload.message)
    hub.create_tag(upload.repo_id, upload.tag, revision)
    return revision


def publish(stage: Stager, *, confirm: bool, hub_for: HubFactory, prefix: str) -> int:
    """Stages into a temporary folder, shows it, and sends only with `confirm`. Exit code."""
    with tempfile.TemporaryDirectory(prefix=prefix) as staging:
        try:
            staged = stage(Path(staging))
            for line in staged.lines:
                sys.stdout.write(f"{line}\n")
            if not confirm:
                sys.stdout.write("Ensaio: nada foi enviado. Use --confirm para publicar.\n")
                return 0
            revision = _send_reporting_only_the_type(staged.upload, hub_for(_token()))
        except PublishError as error:
            sys.stderr.write(f"Publicação recusada: {error}\n")
            return 1
    sys.stdout.write(f'Publicado. Fixar REVISION = "{revision}".\n')
    return 0


def _token() -> SecretStr:
    token = Settings().hf_token
    if token is None:
        raise PublishError("publish.token_missing", "HF_TOKEN")
    return token


def _send_reporting_only_the_type(upload: Upload, hub: ModelHub) -> str:
    try:
        return send(upload, hub)
    except PublishError:
        raise
    except Exception as error:
        # The library's error text can carry request details: only the type is kept.
        raise PublishError("publish.hub_failed", type(error).__name__) from None
