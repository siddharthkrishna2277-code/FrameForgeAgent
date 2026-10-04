"""A failed observation must not be reported as a perception defect.

The Notepad round-trip recorded "no text matching 'FFPROBE7421'" while the typed text had
never reached the screen. That report would have sent a reader to improve OCR when the
defect was in the input path. These tests pin the rule that prevents it.
"""

from __future__ import annotations

import pytest

from frameforge.qa.faultdomain import (
    DOMAIN_REMEDIATION,
    FailureClass,
    FaultDomain,
    classify_failure,
    is_classifiable,
)


class TestRefusesToGuess:
    def test_uninspected_failure_is_undetermined(self):
        fc = classify_failure("text_matches")
        assert fc.domain is FaultDomain.UNDETERMINED
        assert fc.evidence_backed is False

    def test_undetermined_says_what_would_settle_it(self):
        fc = classify_failure("text_matches")
        assert "evidence" in fc.evidence_required.lower()
        assert "present or absent" in fc.evidence_required

    def test_undetermined_points_at_inspection_not_ocr(self):
        """The remediation must not presume the fault is in perception."""
        fc = classify_failure("text_matches")
        assert "not assign a fault domain" in fc.remediation
        assert "OCR" not in fc.remediation or "improve" not in fc.remediation

    def test_claim_without_an_artifact_is_refused(self):
        """Saying 'I looked and it was absent' while naming nothing is not a claim."""
        fc = classify_failure("text_matches", state_observed_present=False)
        assert fc.domain is FaultDomain.UNDETERMINED
        assert fc.evidence_backed is False
        assert "no evidence artifact" in fc.basis

    def test_explicit_false_is_not_enough(self):
        """Even a False must be traceable to something."""
        fc = classify_failure("text_matches", state_observed_present=False,
                              evidence_path="")
        assert fc.domain is FaultDomain.UNDETERMINED


class TestEvidenceBackedClassification:
    def test_state_present_means_perception(self):
        fc = classify_failure("text_matches", state_observed_present=True,
                              evidence_path="frames/17_a.png")
        assert fc.domain is FaultDomain.PERCEPTION
        assert fc.evidence_backed is True
        assert "took effect" in fc.basis

    def test_state_absent_means_delivery(self):
        """The exact Notepad case: text absent from the frame means input never landed."""
        fc = classify_failure("text_matches", state_observed_present=False,
                              evidence_path="frames/43371820_state_menu_file.png")
        assert fc.domain is FaultDomain.DELIVERY
        assert fc.evidence_backed is True
        assert "never took effect" in fc.basis

    def test_the_two_readings_are_remediated_differently(self):
        """That is the entire reason the distinction exists."""
        perception = classify_failure("text_matches", state_observed_present=True,
                                      evidence_path="frames/a.png").remediation
        delivery = classify_failure("text_matches", state_observed_present=False,
                                    evidence_path="frames/b.png").remediation
        assert perception != delivery
        assert "OCR" in perception
        assert "input path" in delivery

    def test_guarded_is_not_a_defect(self):
        fc = classify_failure("text_matches", guarded=True)
        assert fc.domain is FaultDomain.GUARDED
        assert "correct behaviour" in fc.remediation


class TestPixelOnlyConditions:
    @pytest.mark.parametrize("cond", ["text_matches", "text_absent", "region_matches"])
    def test_pixel_conditions_are_not_classifiable_without_evidence(self, cond):
        assert is_classifiable(cond) is False

    @pytest.mark.parametrize("cond", ["anchor_visible", "anchor_absent",
                                      "screen_changed", "no_change", "always"])
    def test_window_level_conditions_can(self, cond):
        assert is_classifiable(cond) is True

    def test_every_domain_has_remediation(self):
        for domain in FaultDomain:
            assert domain in DOMAIN_REMEDIATION
            assert DOMAIN_REMEDIATION[domain].strip()

    def test_round_trips_to_dict(self):
        fc = classify_failure("text_matches", state_observed_present=False,
                              evidence_path="frames/x.png")
        d = fc.to_dict()
        assert d["domain"] == "delivery"
        assert d["evidence_backed"] is True
        assert d["evidence_required"] == "frames/x.png"

    def test_remediation_is_advisory_not_a_code_change(self):
        """G-ROLE-04: Frame Forge names the fault domain, never the fix.

        The report carries no remediation field; this guidance lives in the module and in
        the basis line a human reads. A fix is a code change, which is Herman/Cline's role.
        """
        from frameforge.qa import faultdomain

        text = faultdomain.DOMAIN_REMEDIATION[FaultDomain.DELIVERY]
        assert "input path" in text
        for banned in ("patch", "diff", "fix the code", "edit the file"):
            assert banned not in text.lower()


class TestReportCarriesTheDomain:
    def test_divergence_defaults_to_undetermined(self):
        """A generated report must not imply a perception fault nobody established."""
        from frameforge.qa.report import StepRecord, first_divergence

        rec = StepRecord(name="type_probe_text", disposition="fail",
                         condition="text_matches", detail="no text matching 'X'",
                         expected="X", actual="File\nEdit\nView")
        fd = first_divergence([rec])
        assert fd.fault_domain == "undetermined"
        assert fd.evidence_backed is False
        assert fd.evidence_required, "the report must say what would settle the classification"

    def test_no_divergence_means_no_fault_claim(self):
        from frameforge.qa.report import first_divergence

        fd = first_divergence([])
        assert fd.step == ""
        assert fd.fault_domain == ""


class TestSerialisation:
    def test_domain_survives_to_dict(self):
        from frameforge.qa.report import StepRecord, Report, first_divergence

        rec = StepRecord(name="s", disposition="fail", condition="text_matches")
        r = Report(steps=[rec], failure_digest=[rec],
                   first_divergence=first_divergence([rec]))
        d = r.to_dict()
        assert d["first_divergence"]["fault_domain"] == "undetermined"
        assert "evidence_backed" in d["first_divergence"]

    def test_markdown_shows_the_caveat(self):
        """The caveat must be in the rendered report, not only in the JSON."""
        from frameforge.qa.report import StepRecord, Report, first_divergence

        rec = StepRecord(name="type_probe_text", disposition="fail",
                         condition="text_matches", detail="no text matching 'X'",
                         expected="X", actual="File")
        r = Report(steps=[rec], failure_digest=[rec],
                   first_divergence=first_divergence([rec]))
        md = r.to_markdown()
        assert "Fault domain" in md
        assert "NOT evidence-backed" in md
        assert "does not by itself establish a perception defect" in md
