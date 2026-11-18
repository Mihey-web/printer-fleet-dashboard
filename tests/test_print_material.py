import io
import zipfile
from types import SimpleNamespace as NS
import pytest
from app.services import print_material as pm, job_journal as jj, job_queries as jq, state_store as ss
from app.domain.models import PrinterStatus, PrinterKind, PrinterState


@pytest.mark.parametrize("value", [None, True, 0, -1, "nan", "inf", "broken", 100001])
def test_unknown_grams_not_zero(value):
    assert pm.grams(value) is None


def test_remote_zip_reads_metadata_not_model_and_selects_plate():
    xml = b'<config><plate><metadata key="index" value="1"/><metadata key="weight" value="12.3"/></plate><plate><metadata key="index" value="2"/><metadata key="weight" value="45.6"/></plate></config>'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("model.bin", b"x" * 4_000_000)
        z.writestr("Metadata/slice_info.config", xml)
    content = stream.getvalue()
    opened = []
    def open_range(start):
        opened.append(start)
        return io.BytesIO(content[start:])
    remote = pm.RemoteZip(open_range, len(content))
    with zipfile.ZipFile(remote) as z:
        read = z.read("Metadata/slice_info.config")
    assert remote.total < 200000
    assert len(opened) <= 3   # directory and member, never a range per 64 KB block
    assert pm.slice_grams(read, 2) == 45.6
    assert pm.slice_grams(read, 3) is None
    assert pm.slice_grams(read) is None
    with pytest.raises(ValueError):
        pm.slice_grams(b'<!DOCTYPE config><config/>')


def test_creality_matches_full_current_path_and_sums_filaments():
    report = {"printFileName": "/jobs/test.gcode", "retGcodeFileInfo2": [
        {"path": "/old/test.gcode", "filamentWeight": "99"},
        {"path": "/jobs/test.gcode", "filamentWeight": "10.2,0,3.5"}]}
    assert pm.creality_grams(report) == 13.7
    report['printFileName'] = '/jobs/next.gcode'
    assert pm.creality_grams(report) is None


def test_async_result_cannot_attach_to_new_print(monkeypatch):
    reader = pm.MaterialReader()
    reader.key = (2, "new", "new", "new.3mf")
    monkeypatch.setattr(pm, "read_bambu", lambda *a, **kw: 123)
    reader._read(None, (1, "old", "old", "old.3mf"))
    assert reader.value is None
    device = NS(forge_material=((1,"old","old","old.3mf"),123),
                print_job=NS(subtask_name="new",gcode_file="new.3mf"),
                forge_raw=NS(snapshot=lambda:{"job_seq":2,"job_id":"new"}))
    assert pm.bambu_grams(device) is None


def test_grams_survive_history_restart_cancel_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, 'DB_PATH', str(tmp_path/'history.db'))
    store=ss.StateStore()
    try:
        status=PrinterStatus('test','Test',PrinterKind.BAMBU,True,PrinterState.PRINTING,
                             job_name='Test part',progress_pct=10,planned_filament_g=123.45,preview_key='a'*64,project_key='b'*64)
        store.record_snapshot(status)
        row=store._db.execute('SELECT planned_filament_g FROM printer_history').fetchone()
        assert row[0] == 123.45
        m=jj.JobMachine('test',kind='bambu')
        db=jj.JournalDB(store)
        s=jj.sample_from_status(status, 1000)
        db.apply(m,m.feed(s),now=1000)
        saved=dict(zip([c[0] for c in store._db.execute('SELECT * FROM print_jobs').description],store._db.execute('SELECT * FROM print_jobs').fetchone()))
        m=jj.JobMachine.restore(saved)
        status.state=PrinterState.CANCELLED
        status.planned_filament_g=None
        status.preview_key=None
        status.project_key=None
        s=jj.sample_from_status(status,1100)
        db.apply(m,m.feed(s),now=1100)
        rows=jq.list_jobs(ss.DB_PATH,0,2000)['rows']
        assert rows[0]['planned_filament_g']==123.45
        assert rows[0]['preview_key']=='a'*64
        assert rows[0]['project_key']=='b'*64
        assert rows[0]['result']=='cancelled'
        assert '123,45' in jq.jobs_csv(ss.DB_PATH,0,2000)   # a decimal comma for a Russian Excel
        s=jj.Sample(ts=1200,state='printing',name='Next part',progress=1,seq=2)
        db.apply(m,m.feed(s),now=1200)
        assert m.job.planned_filament_g is None
        assert m.job.preview_key is None
        assert m.job.project_key is None
    finally:
        store._db.close()


def test_creality_one_shot_metadata_survives_next_delta():
    from app.clients.creality_client import CrealityK1Client
    client = CrealityK1Client('example.invalid','test')
    base = {"printFileName":"/jobs/test.gcode","state":1}
    client._parse_report(dict(base,retGcodeFileInfo2=[{"path":"/jobs/test.gcode","filamentWeight":"23.4"}]))
    client._parse_report(dict(base,printProgress=25))
    assert client.data['planned_filament_g']==23.4
    client._parse_report(dict(base,printFileName='/jobs/next.gcode'))
    assert client.data['planned_filament_g'] is None

@pytest.mark.parametrize('fault', [None, 'checksum', 'offset', 'size', 'refused', 'parameter'])
def test_current_memory_slice_validates_transfer(monkeypatch, fault):
    import hashlib, json
    from app.vendor.pybambu import media_sources as ms
    xml = b'<config><plate><metadata key="index" value="1"/><metadata key="weight" value="230.49"/></plate></config>'
    meta = json.dumps({'result': 1 if fault == 'parameter' else 0}).encode()
    replies = [({'result': 1, 'reply': {'total':len(xml), 'mem_dl_param_size':len(meta)}},meta),
               ({'result': 2 if fault == 'refused' else 0, 'reply': {
                   'total':len(xml), 'size':len(xml)+(1 if fault=='size' else 0),
                   'offset':1 if fault=='offset' else 0,
                   'file_md5':'bad' if fault=='checksum' else hashlib.md5(xml).hexdigest()}},xml)]
    class Source:
        closed=False
        def __init__(self,client): self.client=client
        def _connect(self): pass
        def _next_cmd_seq(self): return 7
        def _send_frame(self,magic,payload):
            req, param=payload.split(b'\n\n',1)
            req=json.loads(req);param=json.loads(param)
            assert req['req']['path']=='mem:/16'
            assert param['file_rel_path']=='Metadata/slice_info.config'
            assert param['sequence_id']==req['sequence']==7
        def _read_matching_reply(self,*args,**kwargs):return replies.pop(0)
        def close(self):Source.closed=True
    monkeypatch.setattr(ms,'Tcp6000MediaSource',Source)
    if fault:
        with pytest.raises(ValueError):pm.read_current_slice(NS())
    else:
        assert pm.slice_grams(pm.read_current_slice(NS()),1)==230.49
    assert Source.closed


def test_p1_rest_unsupported_falls_back_to_exact_archive():
    import ftplib
    xml=b'<config><plate><metadata key="index" value="1"/><metadata key="weight" value="174.15"/></plate></config>'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:z.writestr('Metadata/slice_info.config',xml)
    content=stream.getvalue();retrieved=[];logins=[]
    class FTP:
        def __init__(self):logins.append(1)
        def voidcmd(self,cmd):pass
        def size(self,path):
            if path=='/Test.gcode.3mf':return len(content)
            raise ftplib.error_perm('550 missing')
        def transfercmd(self,cmd,rest=None):raise ftplib.error_perm('502')
        def retrbinary(self,cmd,callback,blocksize):retrieved.append(cmd);callback(content)
        def close(self):pass
    client=NS(_device_type='P1S',ftp_connection=FTP)
    assert pm.read_bambu(client,'Test','Test.gcode.3mf')==174.15
    assert retrieved==['RETR /Test.gcode.3mf']
    assert len(logins)==1   # the size probe's login also spools the archive


def _job_archive(weight):
    xml=('<config><plate><metadata key="index" value="1"/><metadata key="weight" value="%s"/></plate></config>' % weight).encode()
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('3D/3dmodel.model',bytes(range(256))*16000)   # ~4 MB that must never be read
        z.writestr('Metadata/project_settings.config',b'{"layer_height": "0.2"}')
        z.writestr('Metadata/slice_info.config',xml)
    return stream.getvalue()


def test_ftp_job_read_uses_one_login_and_few_ranges():
    import ftplib
    content=_job_archive(88.5);logins=[];ranges=[];responses=[]
    class Data:
        def __init__(self,start):self.view=memoryview(content)[start:];self.sent=0
        def recv(self,n):
            chunk=bytes(self.view[self.sent:self.sent+min(n,7000)]);self.sent+=len(chunk);return chunk
        def close(self):pass
    class FTP:
        def __init__(self):logins.append(1)
        def voidcmd(self,cmd):pass
        def size(self,path):
            if path=='/cache/Job.gcode.3mf':return len(content)
            raise ftplib.error_perm('550 missing')
        def transfercmd(self,cmd,rest=None):
            assert cmd=='RETR /cache/Job.gcode.3mf';ranges.append(rest);return Data(rest)
        def voidresp(self):responses.append(1);raise ftplib.error_temp('426 aborted')
        def close(self):pass
    client=NS(_device_type='P1S',ftp_connection=FTP)
    assert pm.read_bambu(client,'Job','Job.gcode.3mf')==88.5
    assert len(logins)==1 and 1<=len(ranges)<=4 and len(responses)==len(ranges)


class _Source:
    """Port-6000 file channel: streams from the requested offset to the end of the file."""
    instances=[];content=b'';fault=None
    def __init__(self,client):_Source.instances.append(self);self.closed=False;self.frames=[]
    def list_files(self,media_type,extensions):
        from app.vendor.pybambu.media_sources import RemoteMediaFile
        return [RemoteMediaFile('Job.gcode.3mf','/data/Job.gcode.3mf',len(self.content),'model','tcp6000')]
    @staticmethod
    def _download_request(remote_file):return {'path':remote_file.path,'offset':0}
    def _send_ctrl_request(self,cmdtype,req):
        assert cmdtype==4 and not self.closed and not self.frames   # one stream per session
        start=req['offset'];step=40000
        for at in range(start,len(self.content),step):
            chunk=self.content[at:at+step];last=at+step>=len(self.content)
            offset=at+(1 if self.fault=='offset' else 0)
            self.frames.append(({'result':0 if last else 1,'reply':{'offset':offset,'size':len(chunk),'total':len(self.content)}},chunk))
        return 9
    def _read_matching_reply(self,cmdtype,seq,timeout=None):return self.frames.pop(0)
    def close(self):self.closed=True


@pytest.mark.parametrize('fault',[None,'offset'])
def test_file_channel_reads_ranges_and_checks_offsets(monkeypatch,fault):
    import ftplib
    from app.vendor.pybambu import media_sources as ms
    class FTP:
        def voidcmd(self,cmd):pass
        def size(self,path):raise ftplib.error_perm('550 missing')
        def close(self):pass
    def refuse(client,member='Metadata/slice_info.config'):raise TimeoutError('old firmware')
    monkeypatch.setattr(pm,'read_current_file',refuse)
    monkeypatch.setattr(ms,'Tcp6000MediaSource',_Source)
    _Source.instances=[];_Source.content=_job_archive(61.25);_Source.fault=fault
    client=NS(_device_type='H2S',ftp_connection=FTP)
    if fault:
        with pytest.raises(ValueError):pm.read_bambu(client,'Job','Job.gcode.3mf')
    else:
        assert pm.read_bambu(client,'Job','Job.gcode.3mf')==61.25
        assert len(_Source.instances)<=4   # listing session + one per jump, never one per block
    assert all(s.closed for s in _Source.instances)


@pytest.mark.parametrize("model", ["H2S", "P2S", "X1C"])
def test_current_slice_preserves_selected_plate_and_avoids_ftp(monkeypatch, model):
    xml=b'<config><plate><metadata key="index" value="1"/><metadata key="weight" value="10"/></plate><plate><metadata key="index" value="2"/><metadata key="weight" value="20"/></plate></config>'
    monkeypatch.setattr(pm,'read_current_slice',lambda client:xml)
    assert pm.read_bambu(NS(_device_type=model),'Test','Metadata/plate_2.gcode')==20


def test_job_without_estimate_is_not_reread_but_failures_are(monkeypatch):
    key = (1, "j", "j", "j.3mf")
    reader = pm.MaterialReader(); reader.key = key
    monkeypatch.setattr(pm, "read_bambu", lambda *a, **kw: None)
    reader._read(None, key)
    assert reader.attempts == pm.MAX_ATTEMPTS and reader.error is None
    reader = pm.MaterialReader(); reader.key = key
    def fail(*a, **kw): raise TimeoutError("/secret/path")
    monkeypatch.setattr(pm, "read_bambu", fail)
    reader._read(None, key)
    assert reader.attempts == 0 and reader.error == "TimeoutError"


def test_failed_memory_read_is_raised_when_no_other_source(monkeypatch):
    def refuse(client, member="Metadata/slice_info.config"): raise TimeoutError("slow")
    monkeypatch.setattr(pm, "read_current_file", refuse)
    client = NS(_device_type="H2S", ftp_connection=None)
    with pytest.raises(TimeoutError):
        pm.read_bambu(client, "", "/data/Metadata/plate_1.gcode")


def test_creality_project_card_and_titles(tmp_path, monkeypatch):
    from app.clients.creality_client import CrealityK1Client
    from app.services import job_previews
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    client = CrealityK1Client('example.invalid', 'test')
    base = {"printFileName": "/jobs/Деталь.STL_PETG_1h2m3s.gcode", "state": 1}
    client._parse_report(dict(base, retGcodeFileInfo2=[{"path": base["printFileName"], "name": "Деталь.STL_PETG_1h2m3s.gcode",
                                                        "filamentWeight": "23.4", "material": "PETG", "timeCost": 3723}]))
    path, summary = client._pending_summary
    client._save_summary(path, summary)
    assert summary["title"] == "Деталь" and summary["time_s"] == 3723
    assert job_previews.valid_key(client.data["project_key"])
    client._parse_report(dict(base, printFileName="/jobs/next.gcode"))
    assert client.data["project_key"] is None and client.data["preview_key"] is None


def test_orphan_previews_are_pruned(tmp_path, monkeypatch):
    import os, time
    from app.services import job_previews
    monkeypatch.setattr(job_previews, "DIRECTORY", tmp_path)
    kept, orphan, fresh = "a" * 64, "b" * 64, "c" * 64
    for name in (kept + ".png", orphan + ".json", fresh + ".png", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    old = time.time() - job_previews.ORPHAN_AGE_S - 10
    for name in (kept + ".png", orphan + ".json", "notes.txt"):
        os.utime(tmp_path / name, (old, old))
    assert job_previews.prune({kept}, time.time()) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([kept + ".png", fresh + ".png", "notes.txt"])


# ── Support weight: the gcode's «; FEATURE: Support…» extrusions (Bambu writes only the total).
_GCODE = (
    "; HEADER_BLOCK_START\n; total filament weight [g] : 9.9\n"
    "; filament_density: 1.25,2.5\n; filament_diameter: 1.75,1.75\n; HEADER_BLOCK_END\n"
    "M83\nT255\n; FEATURE: Outer wall\nG1 X1 Y1 E10000\n"
    "; FEATURE: Support\nG1 X2 Y2 E1000\nG1 E800 F1800\nG1 X3 Y3 E-.5\nG1 Z.4 E500\nG2 X4 Y4 I1 J1 E50 ; arc\n"
    "; FEATURE: Support interface\nG1 X5 Y5 E200\n"
    "T1\n; FEATURE: Support transition\nG1 X6 Y6 E300\n; FEATURE: Sparse infill\nG1 X7 Y7 E5000\n"
)


def _mm3(mm):
    import math
    return mm * math.pi * (1.75 / 2) ** 2 / 1000


@pytest.mark.parametrize("chunk", [7, 64, 1 << 20])
def test_support_grams_counts_support_extrusions_per_filament(chunk):
    import time
    value = pm.support_grams(io.BytesIO(_GCODE.encode()), time.monotonic() + 30, chunk=chunk)
    # T0: 1000 + 50 (arc) + 200 at 1.25 g/cm³; T1: 300 at 2.5. Not: primes, wipes, Z-only, walls, infill, T255.
    assert value == round(_mm3(1250) * 1.25 + _mm3(300) * 2.5, 1) == 5.6


def test_support_grams_unknown_without_header_or_after_deadline():
    import time
    assert pm.support_grams(io.BytesIO(b"; FEATURE: Support\nG1 X1 Y1 E5\n"), time.monotonic() + 30) is None
    assert pm.support_grams(io.BytesIO(_GCODE.encode()), time.monotonic() - 1) is None
    assert pm.support_grams(io.BytesIO(b""), time.monotonic() + 30) is None


def _support_archive(used, weight=9.9, settings=b'{"layer_height": "0.2", "enable_support": "1", "support_type": "normal(auto)"}'):
    xml = ('<config><plate><metadata key="index" value="1"/><metadata key="weight" value="%s"/>'
           '<metadata key="support_used" value="%s"/></plate></config>' % (weight, used)).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/slice_info.config", xml)
        z.writestr("Metadata/project_settings.config", settings)
        z.writestr("Metadata/plate_1.gcode", _GCODE.encode() + b"G1 X0 Y0 F600\n" * 300000)   # ~4.5 MB, over the metadata budget
    return xml, stream.getvalue()


@pytest.mark.parametrize("used", ["true", "false"])
def test_supports_counted_only_for_jobs_with_supports_and_streamed_once(used):
    xml, content = _support_archive(used)
    opened = []
    def open_range(start):
        opened.append(start)
        return io.BytesIO(content[start:])
    summaries = []
    assert pm.read_ranges(open_range, len(content), None, on_project=summaries.append) == 9.9
    assert summaries[0]["layer_mm"] == 0.2 and "support_grams" not in summaries[0]
    if used == "true":
        assert len(summaries) == 2 and summaries[1] == dict(summaries[0], support_grams=5.6)
    else:
        assert len(summaries) == 1
    assert len(opened) <= 4


def test_remote_zip_streaming_drops_passed_blocks():
    content = bytes(range(256)) * 40000
    remote = pm.RemoteZip(lambda start: io.BytesIO(content[start:]), len(content))
    with pytest.raises(ValueError):
        while remote.read(1 << 20):
            pass
    remote = pm.RemoteZip(lambda start: io.BytesIO(content[start:]), len(content))
    remote.allow(len(content))
    got = bytearray()
    while True:
        block = remote.read(1 << 20)
        if not block:
            break
        got.extend(block)
        assert len(remote.cache) <= 2
    assert bytes(got) == content


class _MemoryAndFtp:
    """An X1C/H2S: the memory serves slice_info only; FTP has the job's archive (SD card / stick)."""
    def __init__(self, content, path="/Job.gcode.3mf"):
        import ftplib
        outer = self
        self.content, self.path, self.logins = content, path, 0
        class Data:
            def __init__(self, start): self.view = memoryview(content)[start:]; self.sent = 0
            def recv(self, n):
                chunk = bytes(self.view[self.sent:self.sent + n]); self.sent += len(chunk); return chunk
            def close(self): pass
        class FTP:
            def __init__(self): outer.logins += 1
            def voidcmd(self, cmd): pass
            def size(self, p):
                if outer.content is not None and p == outer.path: return len(outer.content)
                raise ftplib.error_perm("550 missing")
            def transfercmd(self, cmd, rest=None): return Data(rest)
            def voidresp(self): pass
            def close(self): pass
        self.ftp_connection = FTP
        self._device_type = "X1C"


def test_memory_job_gets_settings_and_supports_from_the_card(monkeypatch):
    from app.vendor.pybambu import media_sources as ms
    xml, content = _support_archive("true")
    def memory(client, member="Metadata/slice_info.config"):
        if member == "Metadata/slice_info.config":
            return xml
        raise ValueError("err_no -2")
    monkeypatch.setattr(pm, "read_current_file", memory)
    monkeypatch.setattr(ms, "Tcp6000MediaSource", lambda client: pytest.fail("FTP had the archive"))
    client, events = _MemoryAndFtp(content), []
    value = pm.read_bambu(client, "Job", "Metadata/plate_1.gcode",
                          on_project=lambda s: events.append(("project", s)), on_value=lambda v: events.append(("value", v)))
    assert value == 9.9
    assert [e[0] for e in events] == ["project", "value", "project"]   # short card, grams, then the full one once
    assert "layer_mm" not in events[0][1] and events[0][1]["supports"] == "есть"
    assert events[2][1]["layer_mm"] == 0.2 and events[2][1]["support_grams"] > 0


@pytest.mark.parametrize("case", ["no_storage", "other_job", "broken"])
def test_memory_card_stays_when_the_archive_is_missing_or_another_job(monkeypatch, case):
    from app.vendor.pybambu import media_sources as ms
    xml, _ = _support_archive("true")
    _, other = _support_archive("true", weight=5.5)
    monkeypatch.setattr(pm, "read_current_file", lambda client, member="Metadata/slice_info.config":
                        xml if member.endswith("slice_info.config") else (_ for _ in ()).throw(ValueError("err_no -2")))
    class Empty:
        def __init__(self, client): pass
        def list_files(self, media, ext):
            if case == "broken": raise ConnectionError("channel closed")
            return []
        def close(self): pass
    monkeypatch.setattr(ms, "Tcp6000MediaSource", Empty)
    client = _MemoryAndFtp(other if case == "other_job" else None)
    summaries = []
    assert pm.read_bambu(client, "Job", "Metadata/plate_1.gcode", on_project=summaries.append) == 9.9
    assert len(summaries) == 1 and "layer_mm" not in summaries[0]
