"""一条命令开通一个测试者：建租户 + 绑 Discord + 配报告频道。

后台点四次能做同样的事，但**很容易漏掉报告频道**——漏了不会报错，
只是这个人的自动报告永远发不出来，而且只有他知道。
这里把三件事绑在一起，配不全就不建，不给你留半个。

    uv run python scripts/add_tester.py --name 小王 --discord 123456789012345678 --channel 987654321098765432

绑自己（主账号）用：

    uv run python scripts/add_tester.py --name Noah --discord <你的ID> --tenant default

主账号不需要 --channel：它的投递目标来自 .env，后台改不了也不该改。
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT_DIR / ".env")

from src.runtime.control_store import get_control_store  # noqa: E402
from src.runtime.identity import multi_tenant_enabled  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="开通一个测试者")
    parser.add_argument("--name", required=True, help="显示名称")
    parser.add_argument("--discord", required=True, help="Discord User ID（纯数字）")
    parser.add_argument("--channel", default="", help="他专属的报告频道 ID")
    parser.add_argument("--guild", default="", help="Discord 服务器 ID（可选）")
    parser.add_argument("--tenant", default="", help="绑到已有租户；留空则新建")
    parser.add_argument("--plan", default="trial", help="套餐标签，默认 trial")
    args = parser.parse_args()

    if not args.discord.isdigit():
        print("Discord User ID 必须是纯数字。在 Discord 里打开开发者模式，")
        print("右键头像 → 复制用户 ID。复制到的是用户名的话就是没开开发者模式。")
        return 1

    store = get_control_store()
    store.ensure_default_tenant("Owner")

    if args.tenant:
        tenant = store.get_tenant(args.tenant)
        if tenant is None:
            print(f"没有这个租户：{args.tenant}")
            return 1
    else:
        # 新租户没有报告频道就是白建——报告静默不发，只有他本人发现得了。
        if not args.channel:
            print("新建租户必须带 --channel，否则他的自动报告永远发不出来。")
            print("先在 Discord 里给他建一个**私有**频道：运动记录里带 GPS 起点坐标，")
            print("公共频道等于把每个人的家门口贴出来。")
            return 1
        tenant = store.create_tenant(args.name, plan_code=args.plan)

    tenant_id = str(tenant["id"])

    if args.channel:
        if tenant_id == "default":
            print("主账号的投递目标来自 .env，跳过 --channel。")
        else:
            store.update_tenant(tenant_id, report_channel_id=args.channel)

    store.bind_identity(
        tenant_id, "discord", args.discord, workspace_id=args.guild, label=args.name
    )

    fresh = store.get_tenant(tenant_id)
    print(f"\n已开通：{fresh['name']}")
    print(f"  租户 ID   {tenant_id}")
    print(f"  Discord   {args.discord}")
    print(f"  报告频道  {fresh.get('report_channel_id') or '来自 .env（主账号）'}")
    print(f"  状态      {fresh['status']} / {fresh['subscription_status']}")

    if not multi_tenant_enabled():
        print("\n⚠️  MULTI_TENANT_ENABLED 还是 false —— 这条绑定现在不生效，")
        print("   所有人仍然会被当成 default（也就是你）。")
        print("   确认自己的身份已经绑好之后，再把它改成 true。")

    print("\n让他做的事：")
    print("  1. 加入你的 Discord 服务器")
    print("  2. 在他的频道里发「连接coros」，点链接授权")
    print("  3. 看到「COROS 授权完成」就能用了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
