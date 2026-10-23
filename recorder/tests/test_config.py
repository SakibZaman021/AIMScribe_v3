

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


def test_no_clock_ever_ends_a_consultation_by_default():
    """
    A 15-minute and a 2-minute limit ended real-length consultations on the
    bench. Both are off unless a bench test turns them on.
    """
    import dataclasses
    import config as cfg_module
    fields = {f.name: f.default for f in dataclasses.fields(cfg_module.SecurityConfig)}
    assert fields["no_activity_limit_seconds"] == 0
    assert fields["counselling_limit_seconds"] == 0
