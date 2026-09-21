

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
