import pytest

from deliverius.privacy import (
    MAX_LEDGER_ENTRIES,
    MAX_TEXT_LENGTH,
    MIN_KEY_BYTES,
    TEXT_OMITTED,
    MarkerLedger,
    MaskedText,
    PersonalDataKind,
    mask_text,
    redact_text,
)
from deliverius.shared.errors import InvariantError

KEY = b"k" * MIN_KEY_BYTES
CPF = "529.982.247-25"
CPF_DIGITS = "52998224725"


def mask(text: str, ledger: MarkerLedger | None = None) -> MaskedText:
    return mask_text(text, ledger or MarkerLedger.empty("conversation-1"), KEY)


def nth_cpf_like_email(number: int) -> str:
    return f"cliente{number}@exemplo.com"


def full_ledger() -> MarkerLedger:
    ledger = MarkerLedger.empty("conversation-1")
    for number in range(MAX_LEDGER_ENTRIES):
        ledger = mask(nth_cpf_like_email(number), ledger).ledger
    return ledger


class TestRedactText:
    def test_uses_markers_without_a_number(self) -> None:
        assert redact_text(f"cpf {CPF} e ana@exemplo.com") == "cpf [CPF] e [EMAIL]"

    def test_full_width_cpf_is_hidden(self) -> None:
        assert (
            redact_text("\uff11\uff12\uff13.\uff14\uff15\uff16.\uff17\uff18\uff19-\uff10\uff19")
            == "[CPF]"
        )

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("529\u200b.982\u00ad.247-25", id="invisible characters"),
            pytest.param(
                "\u0665\u0662\u0669.\u0669\u0668\u0662.\u0662\u0664\u0667-\u0662\u0665",
                id="arabic-indic digits",
            ),
            pytest.param("529.982.247\u201325", id="en dash"),
        ],
    )
    def test_disguised_cpf_is_hidden(self, text: str) -> None:
        assert redact_text(text) == "[CPF]"

    def test_text_at_the_limit_is_processed(self) -> None:
        text = "a" * (MAX_TEXT_LENGTH - len(CPF_DIGITS) - 1) + f" {CPF_DIGITS}"

        assert redact_text(text).endswith(" [CPF]")

    def test_text_over_the_limit_is_omitted(self) -> None:
        assert redact_text("a" * (MAX_TEXT_LENGTH + 1)) == TEXT_OMITTED

    def test_text_that_grows_over_the_limit_is_omitted(self) -> None:
        assert redact_text("\ufdfa" * (MAX_TEXT_LENGTH // 2)) == TEXT_OMITTED

    def test_already_masked_text_is_unchanged(self) -> None:
        text = "meu CPF é [CPF_1] e o telefone [TELEFONE_1]"

        assert redact_text(text) == text

    def test_omission_marker_is_unchanged(self) -> None:
        assert redact_text(TEXT_OMITTED) == TEXT_OMITTED


class TestMaskText:
    def test_each_kind_gets_its_own_marker(self) -> None:
        result = mask(
            f"{CPF} 12.ABC.345/01DE-35 4111 1111 1111 1111 +55 11 99570-1543 "
            "ana@exemplo.com 01310-100 Rua das Flores, 25"
        )

        assert result.text == (
            "[CPF_1] [CNPJ_1] [CARTAO_1] [TELEFONE_1] [EMAIL_1] [CEP_1] [ENDERECO_1]"
        )

    def test_same_value_written_differently_keeps_its_marker_across_turns(self) -> None:
        first = mask(f"meu cpf é {CPF}")
        second = mask(f"repito: {CPF_DIGITS}", first.ledger)

        assert first.text == "meu cpf é [CPF_1]"
        assert second.text == "repito: [CPF_1]"
        assert len(second.ledger.entries) == 1

    @pytest.mark.parametrize(
        "same_phone",
        ["11 99570-1543", "+55 11 99570-1543", "5511995701543", "(011) 99570-1543"],
    )
    def test_same_phone_with_and_without_prefixes(self, same_phone: str) -> None:
        first = mask("11995701543")

        assert mask(same_phone, first.ledger).text == "[TELEFONE_1]"

    def test_same_email_in_another_case(self) -> None:
        first = mask("Ana@Exemplo.com")

        assert mask("ana@exemplo.com", first.ledger).text == "[EMAIL_1]"

    def test_two_values_of_the_same_kind_get_different_numbers(self) -> None:
        result = mask("11995701543 e 21987654321")

        assert result.text == "[TELEFONE_1] e [TELEFONE_2]"

    def test_numbers_are_counted_per_kind(self) -> None:
        result = mask(f"{CPF} ana@exemplo.com bia@exemplo.com")

        assert result.text == "[CPF_1] [EMAIL_1] [EMAIL_2]"

    def test_counts_every_occurrence_by_kind_without_the_value(self) -> None:
        result = mask(f"{CPF} {CPF_DIGITS} ana@exemplo.com")

        assert result.counts == {PersonalDataKind.CPF: 2, PersonalDataKind.EMAIL: 1}

    def test_masking_does_not_change_the_previous_ledger(self) -> None:
        ledger = MarkerLedger.empty("conversation-1")

        mask(CPF, ledger)

        assert ledger.entries == ()

    def test_text_without_personal_data(self) -> None:
        result = mask("cadê meu pedido?")

        assert result.text == "cadê meu pedido?"
        assert result.ledger.entries == ()
        assert result.counts == {}

    def test_marker_typed_by_the_customer_reveals_nothing(self) -> None:
        assert mask("meu cpf é [CPF_7]").text == "meu cpf é [CPF_7]"


class TestIrreversibility:
    @pytest.mark.parametrize("value", [CPF, CPF_DIGITS])
    def test_serialized_ledger_holds_neither_the_value_nor_its_digits(self, value: str) -> None:
        serialized = repr(mask(value).ledger)

        assert CPF not in serialized
        assert CPF_DIGITS not in serialized

    def test_ledgers_of_two_conversations_cannot_be_linked(self) -> None:
        first = mask(CPF, MarkerLedger.empty("conversation-1"))
        second = mask(CPF, MarkerLedger.empty("conversation-2"))

        assert first.ledger.entries[0].fingerprint != second.ledger.entries[0].fingerprint

    def test_fingerprint_depends_on_the_key(self) -> None:
        ledger = MarkerLedger.empty("conversation-1")

        first = mask_text(CPF, ledger, b"a" * MIN_KEY_BYTES)
        second = mask_text(CPF, ledger, b"b" * MIN_KEY_BYTES)

        assert first.ledger.entries[0].fingerprint != second.ledger.entries[0].fingerprint

    def test_short_key_is_refused(self) -> None:
        with pytest.raises(InvariantError) as error:
            mask_text(CPF, MarkerLedger.empty("conversation-1"), b"k" * (MIN_KEY_BYTES - 1))

        assert error.value.rule == "masking.key_too_short"

    def test_key_of_the_minimum_length_is_accepted(self) -> None:
        assert mask_text(CPF, MarkerLedger.empty("c"), b"k" * MIN_KEY_BYTES).text == "[CPF_1]"


class TestLedgerLimit:
    def test_last_value_that_fits_still_gets_a_number(self) -> None:
        ledger = MarkerLedger.empty("conversation-1")
        for number in range(MAX_LEDGER_ENTRIES - 1):
            ledger = mask(nth_cpf_like_email(number), ledger).ledger

        result = mask(CPF, ledger)

        assert result.text == "[CPF_1]"
        assert len(result.ledger.entries) == MAX_LEDGER_ENTRIES

    def test_new_value_on_a_full_ledger_gets_a_marker_without_a_number(self) -> None:
        result = mask(CPF, full_ledger())

        assert result.text == "[CPF]"
        assert len(result.ledger.entries) == MAX_LEDGER_ENTRIES

    def test_known_value_on_a_full_ledger_keeps_its_marker(self) -> None:
        result = mask(nth_cpf_like_email(0), full_ledger())

        assert result.text == "[EMAIL_1]"


class TestTextLimit:
    def test_text_over_the_limit_is_omitted_and_the_ledger_is_kept(self) -> None:
        ledger = mask(CPF).ledger

        result = mask("a" * (MAX_TEXT_LENGTH + 1), ledger)

        assert result.text == TEXT_OMITTED
        assert result.ledger == ledger
        assert result.counts == {}

    def test_text_that_grows_over_the_limit_is_omitted(self) -> None:
        assert mask("\ufdfa" * (MAX_TEXT_LENGTH // 2)).text == TEXT_OMITTED
