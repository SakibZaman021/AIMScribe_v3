

def test_a_bench_agent_keeps_its_state_out_of_the_way(monkeypatch, tmp_path):
    """
    AIMS_DATA_DIR moves keys, enrolment and spool somewhere else, so a bench
    agent can run on a machine that already has one enrolled without touching
    what that one holds.
    """
    import config as config_module

    monkeypatch.setenv("AIMS_DATA_DIR", str(tmp_path / "bench"))
    assert config_module.data_dir() == tmp_path / "bench"

    monkeypatch.delenv("AIMS_DATA_DIR")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "machine"))
    assert config_module.data_dir() == tmp_path / "machine" / "AIMScribe"


def test_the_recording_ends_two_minutes_after_the_prescription():
    """
    Two minutes of counselling after the prescription, and up to ten seconds
    more while someone is speaking. A follow-up with no prescription has no
    clock: the next patient ends it.
    """
    import dataclasses
    import config as cfg_module
    fields = {f.name: f.default for f in dataclasses.fields(cfg_module.SecurityConfig)}
    assert fields["counselling_limit_seconds"] == 120
    assert fields["end_grace_seconds"] == 10
    assert fields["no_activity_limit_seconds"] == 0
