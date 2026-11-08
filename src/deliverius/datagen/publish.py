"""Publishing a generated dataset to the Hugging Face Hub, with its dataset card.

The card is written in English because the dataset is public; what the CLI tells the operator is
Portuguese and lives in `cli/`. Nothing leaves the machine without an explicit confirmation, and the
token is never part of a log line or an error: a failure of the hub reports only the exception type.
"""

import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Final, Protocol

import structlog
from huggingface_hub import HfApi
from pydantic import SecretStr

from deliverius.datagen.output import MANIFEST_FILE, verify_dataset
from deliverius.datagen.records import Intent, Manifest
from deliverius.shared import DeliveriusError

logger = structlog.get_logger(__name__)

LICENSE_ID: Final = "cc-by-4.0"
CARD_FILE: Final = "README.md"
TOKEN_VARIABLE: Final = "HF_TOKEN"  # noqa: S105 - the name of the variable, not a secret

# Configs of the card, in order, with the task each one serves. Anything else in the folder
# (the world, the ground truth) is uploaded but is not a config.
_CONFIG_TASKS: Final = {
    "messages": ("text-classification", "token-classification"),
    "messages_holdout": ("text-classification",),
    "fraud_history": ("tabular-classification",),
    "policy_qa": ("text-retrieval",),
    "e2e_scenarios": (),
}
_MESSAGE_SETS: Final = ("messages", "messages_holdout")


class PublishError(DeliveriusError):
    """Publishing cannot go on; `rule` is a stable code and `detail` never holds a secret."""

    def __init__(self, rule: str, detail: str | None = None) -> None:
        super().__init__(rule if detail is None else f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """What the by-hand review of a sample found, for the card's limitations; both optional."""

    defect_rate: float | None = None
    sample_size: int | None = None


@dataclass(frozen=True, slots=True)
class PlannedFile:
    name: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PublishPlan:
    directory: Path
    manifest: Manifest
    files: tuple[PlannedFile, ...]
    card: str

    @property
    def tag(self) -> str:
        return f"v{self.manifest.datagen_version}"


class DatasetHub(Protocol):
    """The four things publishing asks of the Hub; a test replaces it."""

    def create_dataset_repo(self, repo_id: str) -> None:
        """Creates a public dataset repository; nothing happens if it already exists."""
        ...

    def tags(self, repo_id: str) -> frozenset[str]: ...

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> None: ...

    def create_tag(self, repo_id: str, tag: str) -> None: ...


type HubFactory = Callable[[SecretStr], DatasetHub]


class HuggingFaceHub:
    """`DatasetHub` on `huggingface_hub.HfApi`, already holding the token."""

    def __init__(self, api: HfApi) -> None:
        self._api = api

    def create_dataset_repo(self, repo_id: str) -> None:
        self._api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)

    def tags(self, repo_id: str) -> frozenset[str]:
        refs = self._api.list_repo_refs(repo_id, repo_type="dataset")
        return frozenset(ref.name for ref in refs.tags)

    def upload_folder(self, repo_id: str, folder: Path, message: str) -> None:
        self._api.upload_folder(
            repo_id=repo_id, repo_type="dataset", folder_path=folder, commit_message=message
        )

    def create_tag(self, repo_id: str, tag: str) -> None:
        self._api.create_tag(repo_id, tag=tag, repo_type="dataset")


def huggingface_hub_for(token: SecretStr) -> DatasetHub:
    return HuggingFaceHub(HfApi(token=token.get_secret_value()))


def plan_publication(
    directory: Path, review: ReviewResult | None = None, faker_version: str | None = None
) -> PublishPlan:
    """Checks every file against the manifest and writes the card. Sends nothing.

    Raises `DatasetError` if a file does not match its hash.
    """
    manifest = verify_dataset(directory)
    if CARD_FILE in manifest.files:
        raise PublishError("publish.card_name_taken", CARD_FILE)
    files = tuple(
        PlannedFile(name, (directory / name).stat().st_size)
        for name in (*sorted(manifest.files), MANIFEST_FILE)
    )
    card = render_card(manifest, review or ReviewResult(), faker_version or _faker_version())
    return PublishPlan(directory, manifest, files, card)


def announce_dry_run(plan: PublishPlan, repo_id: str) -> None:
    logger.info("datagen.publish_dry_run", repo=repo_id, files=len(plan.files) + 1, tag=plan.tag)


def publish(plan: PublishPlan, repo_id: str, token: SecretStr | None, hub_for: HubFactory) -> None:
    """Uploads the files and the card, then tags the commit with the generator version.

    Without a token nothing is built or sent. A tag that already exists is refused before the
    upload, so a published version is never overwritten. Any failure of the hub becomes a
    `PublishError` that names only the exception type.
    """
    if token is None or not token.get_secret_value():
        raise PublishError("publish.token_missing", TOKEN_VARIABLE)
    try:
        _send(plan, repo_id, hub_for(token))
    except PublishError:
        raise
    except Exception as error:
        # The library's error text can carry request details: only the type is kept.
        raise PublishError("publish.hub_failed", type(error).__name__) from None
    logger.info("datagen.published", repo=repo_id, tag=plan.tag, files=len(plan.files) + 1)


def _send(plan: PublishPlan, repo_id: str, hub: DatasetHub) -> None:
    hub.create_dataset_repo(repo_id)
    if plan.tag in hub.tags(repo_id):
        raise PublishError("publish.tag_exists", plan.tag)
    with tempfile.TemporaryDirectory(prefix="deliverius-publish-") as staging:
        folder = _stage(plan, Path(staging))
        hub.upload_folder(repo_id, folder, f"Dataset {plan.tag}")
    hub.create_tag(repo_id, plan.tag)


def _stage(plan: PublishPlan, folder: Path) -> Path:
    for planned in plan.files:
        target = folder / planned.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plan.directory / planned.name, target)
    (folder / CARD_FILE).write_bytes(plan.card.encode("utf-8"))
    return folder


def _faker_version() -> str:
    return metadata.version("faker")


def render_card(manifest: Manifest, review: ReviewResult, faker_version: str) -> str:
    sections = [
        _front_matter(manifest),
        f"# Deliverius synthetic dataset ({manifest.datagen_version})",
        _summary(),
        _synthetic_notice(),
        _how_generated(manifest, faker_version),
        _sizes(manifest),
        _fields(manifest),
        _limitations(manifest, review),
        _license(),
    ]
    return "\n\n".join(sections) + "\n"


def _present_configs(manifest: Manifest) -> list[str]:
    return [
        name
        for name in _CONFIG_TASKS
        if any(file.startswith(f"{name}/") for file in manifest.files)
    ]


def _front_matter(manifest: Manifest) -> str:
    configs = _present_configs(manifest)
    tasks = list(dict.fromkeys(task for name in configs for task in _CONFIG_TASKS[name]))
    lines = [
        "---",
        f"license: {LICENSE_ID}",
        "language:",
        "- pt",
        "pretty_name: Deliverius Synthetic Customer Support",
        "annotations_creators:",
        "- machine-generated",
        "language_creators:",
        "- machine-generated",
        "multilinguality:",
        "- monolingual",
        "source_datasets:",
        "- original",
    ]
    if tasks:
        lines += ["task_categories:", *(f"- {task}" for task in tasks)]
    lines += ["tags:", "- synthetic", "- customer-support", "- fraud-detection", "- pii-masking"]
    if configs:
        lines.append("configs:")
        for name in configs:
            lines += [f"- config_name: {name}", "  data_files:"]
            for path in _config_files(manifest, name):
                lines += [f"  - split: {Path(path).stem}", f"    path: {path}"]
    lines.append("---")
    return "\n".join(lines)


def _config_files(manifest: Manifest, config: str) -> list[str]:
    return sorted(file for file in manifest.files if file.startswith(f"{config}/"))


def _summary() -> str:
    return (
        "Synthetic data for Deliverius, a fictional delivery app, and Deli, its customer-support "
        "agent: customer messages labelled by intent, a refund-claim history labelled for fraud, "
        "policy questions with the section that answers them, and end-to-end scenarios with the "
        "expected final state. Every label is true by construction: it comes from the scenario "
        "the code drew, never from a language model."
    )


def _synthetic_notice() -> str:
    return "\n".join(
        [
            "## Synthetic data notice",
            "",
            "Everything here is made up. Customers, restaurants, orders and messages describe no "
            "real person, company or event. Names, CPF numbers (they have valid check digits but "
            "belong to nobody), phone numbers, e-mail addresses and street addresses are generated "
            "with Faker. They exist to test personal-data masking and must not be treated as real "
            "data. The prompt-injection sentences inside some messages are test data.",
        ]
    )


def _how_generated(manifest: Manifest, faker_version: str) -> str:
    commit = manifest.commit + (" (with uncommitted changes)" if manifest.dirty else "")
    lines = [
        "## How it was generated",
        "",
        "Five layers, in order. Only the fourth uses a language model.",
        "",
        "1. A simulator draws customers (honest, opportunist or fraudster profiles), restaurants "
        "and six months of orders with delivery timelines and incidents.",
        "2. Claims come from the profile and the incidents; the fraud label says whether the claim "
        "has no real incident behind it. A small fraction of labels is flipped on purpose.",
        "3. The expected resolution of each claim is computed by the same policy engine the "
        "application uses.",
        "4. A language model writes the wording of customer messages from the facts of a drawn "
        "scenario. It sees only markers such as `[[CPF]]`; the code fills them with fake values "
        "and records where each personal datum landed. The intent label is the scenario's.",
        "5. End-to-end scenarios are built without a model, with the expected final state taken "
        "from the policy engine.",
        "",
        f"- Generator version: `{manifest.datagen_version}`, seed `{manifest.seed}`, "
        f"commit `{commit}`.",
        f"- Reference instant of the simulated period: `{manifest.reference_instant.isoformat()}`.",
        f"- Faker version: `{faker_version}`.",
    ]
    lines += _models_table(manifest.models) + _prompts_table(manifest)
    return "\n".join(lines)


def _models_table(models: Mapping[str, str]) -> list[str]:
    if not models:
        return []
    return [
        "",
        "| Role | Model |",
        "| --- | --- |",
        *(f"| {r} | `{m}` |" for r, m in models.items()),
    ]


def _prompts_table(manifest: Manifest) -> list[str]:
    if not manifest.prompt_versions:
        return []
    rows = [
        f"| {name} | {version} | `{manifest.prompt_hashes.get(name, 'n/a')}` |"
        for name, version in manifest.prompt_versions.items()
    ]
    return ["", "| Prompt | Version | SHA-256 |", "| --- | --- | --- |", *rows]


def _sizes(manifest: Manifest) -> str:
    lines = ["## Sizes and splits", ""]
    lines += ["| Set | Rows |", "| --- | --- |"]
    lines += [f"| {name} | {rows} |" for name, rows in manifest.counts.items()]
    lines += _split_rules(manifest)
    lines += _intent_table(manifest)
    return "\n".join(lines)


def _split_rules(manifest: Manifest) -> list[str]:
    lines: list[str] = []
    if any(key.startswith("messages.") for key in manifest.counts):
        lines += [
            "",
            "Messages are split 70/15/15 by scenario, stratified by intent: the variations of one "
            "scenario are always on the same side.",
        ]
    if manifest.fraud_test_cutoff is not None:
        lines += [
            "",
            "The fraud history is split by time: the test set is everything at or after "
            f"`{manifest.fraud_test_cutoff.isoformat()}`. The fraud rate among claims is "
            f"{_percent(manifest.fraud_rate)} and the fraction of flipped labels is "
            f"{_percent(manifest.label_noise_fraction)}.",
        ]
    return lines


def _intent_table(manifest: Manifest) -> list[str]:
    sets = list(manifest.intent_counts)
    if not sets:
        return []
    header = ["", "Messages per intent:", "", f"| Intent | {' | '.join(sets)} |"]
    header.append("| --- |" + " --- |" * len(sets))
    rows = [
        f"| {intent.value} | "
        + " | ".join(str(manifest.intent_counts[name].get(intent.value, 0)) for name in sets)
        + " |"
        for intent in Intent
    ]
    return [*header, *rows]


def _fields(manifest: Manifest) -> str:
    descriptions = {
        "messages": "`text`, `intent` (one of 11), `split`, `scenario_id`, `pii_spans` (kind, "
        "start, end of each personal datum in `text`), `has_injection`, `persona`.",
        "messages_holdout": "The same fields as `messages`, written by a model of another "
        "family from scenarios that share nothing with the main set. Test only.",
        "fraud_history": "One claim per row: order, customer, instant, reason, claimed items and "
        "amount, the policy resolution, and `is_fraud`. No customer profile and no flipped-label "
        "mark: those are in `fraud_ground_truth/`, for analysis only.",
        "policy_qa": "`question`, and the `document` and `section` of the policy that answers it.",
        "e2e_scenarios": "`flow` (F1 to F7), the `initial_state` (customer, order, prior "
        "actions), the customer's `goal`, and the `expected` final state.",
    }
    lines = ["## Fields", ""]
    lines += [f"- **{name}**: {descriptions[name]}" for name in _present_configs(manifest)]
    return "\n".join(lines)


def _limitations(manifest: Manifest, review: ReviewResult) -> str:
    lines = [
        "## Known limitations",
        "",
        "- The data is synthetic: rates, prices and behaviours are plausible, not calibrated "
        "against a real service.",
        "- The intent of a message is the scenario's. The model may write something vaguer, or off "
        "topic; that is also what real messages do, and the manual review below counts it.",
        "- All messages of one set come from one model, so their style is less varied than that "
        "of real customers. `messages_holdout` exists to measure how much a classifier memorised "
        "that style.",
        "- Portuguese (Brazil) only.",
        f"- {_review_sentence(review)}",
    ]
    lines += _discard_lines(manifest)
    return "\n".join(lines)


def _review_sentence(review: ReviewResult) -> str:
    if review.defect_rate is None:
        return "Manual review defect rate: not measured yet."
    sample = "" if review.sample_size is None else f" (sample of {review.sample_size} messages)"
    return f"Manual review defect rate: {_percent(review.defect_rate)}{sample}."


def _discard_lines(manifest: Manifest) -> list[str]:
    lines: list[str] = []
    for name in _MESSAGE_SETS:
        kept = manifest.intent_counts.get(name)
        if kept is None:
            continue
        discarded = _discards_by_intent(manifest.discards, name)
        total_kept, total_dropped = sum(kept.values()), sum(discarded.values())
        worst_intent, worst_rate = max(
            ((i, _rate(discarded.get(i, 0), n)) for i, n in kept.items()),
            key=lambda item: item[1],
        )
        lines.append(
            f"- Discard rate of `{name}` variations: "
            f"{_percent(_rate(total_dropped, total_kept))} overall "
            f"({total_dropped} discarded, {total_kept} kept); highest for `{worst_intent}`: "
            f"{_percent(worst_rate)}."
        )
    return lines


def _discards_by_intent(discards: Mapping[str, int], message_set: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for key, count in discards.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] == message_set:
            totals[parts[2]] = totals.get(parts[2], 0) + count
    return totals


def _rate(discarded: int, kept: int) -> float:
    total = discarded + kept
    return discarded / total if total else 0.0


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _license() -> str:
    return (
        "## License\n\nCreative Commons Attribution 4.0 International "
        "([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)). "
        "The generator version, seed and commit above reproduce the files."
    )
