# Rows for the exit-4 stderr line in the merged; not merged; not known shape (next_steps._merge_partial_line).
# The expected text is the helper's, which a design note pins.
from gr2.python_cli import next_steps

SHA_A = "a" * 40
SHA_B = "b" * 40


def _m(key, final, code=None, merged=None, state=None):
    row = {"key": key, "final": final}
    if code:
        row["refused"] = f"{code}: detail"
    if merged:
        row["merged"] = merged
    if state:
        row["state"] = state
    return row


def _line(*members):
    return next_steps._merge_partial_line(4, {"members": list(members)})


def test_the_partial_says_which_members_landed_on_stderr():
    first = _line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unmerged", "feature_moved_before_push"))
    assert first == f"partial: merged alpha ({SHA_A[:12]}); not merged beta (feature_moved_before_push)"
    again = _line(_m("alpha", "merged", merged=SHA_A, state="already_merged"),
                  _m("beta", "unmerged", "feature_moved"))
    assert again == f"partial: merged alpha ({SHA_A[:12]}; earlier run); not merged beta (feature_moved)"


def test_a_three_member_partial_with_the_middle_refused():
    line = _line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unmerged", "push_rejected"),
                 _m("gamma", "unknown"))
    assert line == f"partial: merged alpha ({SHA_A[:12]}); not merged beta (push_rejected); not known gamma (unknown)"


def test_partial_prints_only_with_a_merged_and_an_unmerged_member():
    assert _line(_m("alpha", "unknown"), _m("beta", "unknown")) is None
    assert _line(_m("alpha", "unmerged", "base_moved"), _m("beta", "unmerged", "base_moved")) is None
    assert _line(_m("alpha", "merged", merged=SHA_A)) is None
    assert (_line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unknown"))
            == f"partial: merged alpha ({SHA_A[:12]}); not known beta (unknown)")
    assert next_steps._merge_partial_line(0, {"members": [_m("alpha", "merged", merged=SHA_A)]}) is None


def test_a_run_of_one_member_that_landed_with_a_code_prints_the_line():
    assert (_line(_m("alpha", "merged", "stranded", merged=SHA_A))
            == f"partial: merged alpha ({SHA_A[:12]}; stranded)")


def test_an_unknown_final_is_named_unknown_never_not_merged():
    line = _line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unknown"), _m("gamma", "unmerged", "base_moved"))
    assert line == f"partial: merged alpha ({SHA_A[:12]}); not merged gamma (base_moved); not known beta (unknown)"
    missing = _line(_m("alpha", "merged", merged=SHA_A), {"key": "beta"}, _m("gamma", "unmerged", "base_moved"))
    assert missing == f"partial: merged alpha ({SHA_A[:12]}); not merged gamma (base_moved); not known beta (unknown)"


def test_a_member_with_no_refusal_code_says_so_not_its_final():
    assert (_line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unmerged"))
            == f"partial: merged alpha ({SHA_A[:12]}); not merged beta (no refusal code)")


def test_the_middle_member_and_an_earlier_run_are_placed_by_their_own_final():
    line = _line(_m("alpha", "unmerged", "feature_moved"),
                 _m("beta", "merged", merged=SHA_B, state="already_merged"),
                 _m("gamma", "unmerged", "base_moved"))
    assert line == (f"partial: merged beta ({SHA_B[:12]}; earlier run); "
                    "not merged alpha (feature_moved), gamma (base_moved)")


def test_a_key_or_code_cannot_forge_a_line():
    line = _line(_m("alpha\rpartial: merged everything; not merged none", "merged", merged=SHA_A),
                 _m("beta\n", "unmerged", "x\x1b[2K‮"))
    assert "\r" not in line and "\n" not in line and "\x1b" not in line and "‮" not in line


def test_alpha_lands_then_betas_remote_vanishes_and_stderr_names_both():
    line = _line(_m("alpha", "merged", merged=SHA_A), _m("beta", "unknown", "remote_unreachable"))
    assert line == f"partial: merged alpha ({SHA_A[:12]}); not known beta (remote_unreachable)"
