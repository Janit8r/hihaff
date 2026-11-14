# Webshell payload 库（种子）

上游 `200210xsb/artex-full` **不分发 payload 库**（`mcp/install-mcp.sh` 注释里明确写了
「可直接落地的马不入库」）。裸 runner 上 `server/webshell.go` 的 `webshellPayloadRoot()`
指向一个不存在的目录，于是 `GET /api/webshell/payloads` 直接 500
`payload 目录不存在: ...`。

部署工作流把这个目录整体铺到 `${STATE_DIR}/payloads/webshell`（由环境变量
`ARTEX_WEBSHELL_PAYLOAD_DIR` 指定），并在每轮把本目录内容同步过去：

- 目录结构必须有 `php/` `jsp/` `aspx/` 三个子目录；文件名后缀必须与目录同名
  （`php/*.php`、`jsp/*.jsp`、`aspx/*.aspx`），否则后端不会列入。
- 随仓库分发的同名文件每轮覆盖；**其它文件一律保留**，且整个目录随
  `.artex-state` 缓存跨轮保留，不会每轮清空。
- 这里只放 3 个**无命令执行能力的连通性探针**（只回显一个标记），
  用来验证挂载、检索、读取与元信息解析链路是通的，不是可用的马。

## 加自己的 payload

按 `<类型>/<文件名>.<类型>` 放进本目录，push 后 `deploy/**` 会触发新一轮部署。
文件开头可以写一段 `/* ... */` 注释提供元信息（后端只取前 80 行、每类只取第一次出现）：

```
/*
 * @summary 一句话说明
 * @scene   适用场景
 * @perm    需要的权限
 * @bypass  绕过点
 * @param   动态参数说明
 * @mcp     相关 MCP 工具
 */
<?php ... ?>
```

前端「Payload 库」抽屉和 MCP 的 `payload_get` 返回时会**剥掉这段注释**，
只给载荷正文；`payload_list` 仍会展示元信息。含 `__ARTEX_L__` 占位符的加密马
由后端单独渲染，这里不涉及。

> 注意：如果 payload 需要 API key 之类的敏感内容，不要提交到仓库；
> 改用常驻部署（README 第 7 节）把文件直接放到主机上。
