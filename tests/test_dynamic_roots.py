import os

from zhiqiu.files import FileRoots, merge_roots

from test_agent import make_agent, say, tool, tool_rows


def offered(model):
    return {item["function"]["name"] for item in model.seen[-1][2]}


def test_a_root_added_between_turns_is_readable_next_turn_and_gone_after_removal(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "堆.md").write_text("堆是一棵完全二叉树。", encoding="utf-8")
    target = str(extra / "堆.md")
    store, model, io, agent = make_agent(tmp_path, [tool("read_file", {"path": target}, "a"), say("读不到")])
    agent.handle("读一下堆的笔记")
    assert [r["status"] for r in tool_rows(store)] == ["denied"]

    store.add_root("read", str(extra.resolve()), 1, False)
    model.rounds = [tool("read_file", {"path": target}, "b"), say("读到了")]
    agent.handle("再读一次")
    assert [r["status"] for r in tool_rows(store)] == ["denied", "ok"]
    assert str(extra.resolve()) in agent._context()

    store.remove_root("read", str(extra.resolve()))
    model.rounds = [tool("read_file", {"path": target}, "c"), say("又读不到了")]
    agent.handle("第三次")
    assert [r["status"] for r in tool_rows(store)] == ["denied", "ok", "denied"]


def test_roots_do_not_change_in_the_middle_of_a_turn(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "a.md").write_text("内容", encoding="utf-8")
    store, model, io, agent = make_agent(tmp_path, [])

    class AddsRootMidTurn:
        """The first round adds a root (as another process might); the read in round two still uses the old ones."""
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def tool_round(self, *args, **kwargs):
            if not store.roots("read"):
                store.add_root("read", str(extra.resolve()), 1, False)
            return self.inner.tool_round(*args, **kwargs)

    model.rounds = [tool("read_file", {"path": str(extra / "a.md")}, "a"), say("好")]
    agent.service.model = AddsRootMidTurn(model)
    agent.handle("读 a.md")
    assert [r["status"] for r in tool_rows(store)] == ["denied"]


def test_write_file_is_offered_only_while_a_write_root_exists(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    store, model, io, agent = make_agent(tmp_path, [say("好")])
    agent.handle("你好")
    assert "write_file" not in offered(model) and "未配置，不能写文件" in agent._context()

    store.add_root("write", str(out.resolve()), 0, False)
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "w"), say("写好了")]
    asked = []
    io.confirm_tool = lambda spec, args: asked.append(spec.name) or True
    agent.handle("写一条笔记")
    assert asked == ["write_file"] and (out / "a.md").read_text(encoding="utf-8") == "笔记"
    assert agent.service.write_roots == [out.resolve()]

    store.remove_root("write", str(out.resolve()))
    model.rounds = [say("好")]
    agent.handle("再聊")
    assert "write_file" not in offered(model) and agent.service.write_roots == []


def test_environment_roots_come_first_and_duplicates_are_dropped(tmp_path):
    env = tmp_path / "env"
    added = tmp_path / "added"
    env.mkdir()
    added.mkdir()
    store, model, io, agent = make_agent(tmp_path, [])
    store.add_root("read", str(added.resolve()), 0, False)
    roots = FileRoots([env, added], [])
    assert roots.current(store, "read") == [env, added]
    listing = roots.listing(store)
    assert [(item["source"], item["path"]) for item in listing["read"]] == [("env", str(env)), ("env", str(added))]
    assert merge_roots([env], [str(env)], [env / "."]) == [env]
    if os.name == "nt":
        assert merge_roots([env], [str(env).upper()]) == [env]


def test_no_tool_can_change_the_roots(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    for registry in (agent.registry, agent.service.tools):
        for name in registry.names():
            spec = registry.get(name)
            text = f"{name} {spec.description}".lower()
            assert "root" not in name and "白名单" not in text and "allowlist" not in text
    file_tools = {name for name in agent.registry.names() if agent.registry.get(name).source == "zhiqiu.files"}
    assert file_tools == {"read_file", "write_file"}
