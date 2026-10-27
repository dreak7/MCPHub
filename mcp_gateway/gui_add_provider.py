# -*- coding: utf-8 -*-
"""MCPHub 可视化配置管理向导 (Visual Configuration Tool).

提供纯原生零外部依赖的 Windows 桌面可视化配置界面，
用于直观增减搜索引擎、调整优先级、同步写入 API Key，或自定义全新能力分类。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
import tkinter as tk
from typing import Any
from tkinter import ttk, messagebox

# 启用 Windows 高分屏 (High DPI) 清晰度渲染，避免界面模糊
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# =========================================================================
# 【功能块：项目核心路径自适应检测与定位】
# - 输入：当前脚本所在文件路径 (__file__)
# - 功能：自动解析并定位 MCPHub 项目根目录、config 目录、providers 目录与 .env 路径
# - 输出：PROJECT_ROOT, CONFIG_DIR, PROVIDERS_DIR, ENV_PATH (Path 对象)
# =========================================================================
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROVIDERS_DIR = CONFIG_DIR / "providers"
ENV_PATH = CONFIG_DIR / ".env"
ENV_EXAMPLE_PATH = CONFIG_DIR / ".env.example"

# 常见引擎适配器协议与默认接口对照表
DEFAULT_ENDPOINTS = {
    "tavily-search": "https://api.tavily.com/search",
    "exa": "https://api.exa.ai/search",
    "jina": "https://s.jina.ai",
    "bocha-search": "https://api.bocha.cn/v1/web-search",
    "anysearch": "https://api.anysearch.com/v1/search",
    "context7": "https://context7.com/api/v3/search",
}


# =========================================================================
# 【功能块：扫描并提取所有已存在的能力分类与对应的配置文件】
# - 输入：PROVIDERS_DIR (Path): 存放提供商 JSON 配置文件的目录路径
# - 功能：遍历扫描目录下所有 .json 文件，解析其中的 capabilities 节点，
#         建立能力名称与配置文件的映射关系
# - 输出：dict[str, tuple[Path, dict]]: {能力名: (文件路径, 能力详情字典)}
# =========================================================================
def scan_existing_capabilities() -> dict[str, tuple[Path, dict]]:
    capabilities: dict[str, tuple[Path, dict]] = {}
    if not PROVIDERS_DIR.exists():
        return capabilities

    for json_file in sorted(PROVIDERS_DIR.glob("*.json")):
        try:
            content = json.loads(json_file.read_text(encoding="utf-8"))
            caps = content.get("capabilities", {})
            if isinstance(caps, dict):
                for cap_name, cap_def in caps.items():
                    if isinstance(cap_def, dict):
                        capabilities[cap_name] = (json_file, cap_def)
        except Exception:
            continue
    return capabilities


# =========================================================================
# 【功能块：安全同步更新 .env 环境变量文件】
# - 输入：key_name (str): 环境变量名；key_value (str): 密钥内容
# - 功能：若 .env 不存在则自动从 .env.example 复制初始化；
#         若变量已存在则原地修改，若不存在则追加到末尾，全程保持 UTF-8 编码
# - 输出：无
# =========================================================================
def save_api_key_to_dotenv(key_name: str, key_value: str) -> None:
    if not key_name or not key_value:
        return

    if not ENV_PATH.exists():
        if ENV_EXAMPLE_PATH.exists():
            ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("# MCPHub Environment Variables\n", encoding="utf-8")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines: list[str] = []
    # 值内的换行会截断本行并向 .env 注入额外键值对，破坏配置结构，先行剔除
    safe_value = "".join(key_value.split())

    pattern = re.compile(rf"^\s*{re.escape(key_name)}\s*=")
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key_name}={safe_value}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key_name}={safe_value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# =========================================================================
# 【功能块：可视化配置管理向导窗口主类】
# - 输入：root (tk.Tk): Tkinter 根窗口实例
# - 功能：构建清晰直观的双轨制（加入已有分类 / 新建自定义能力分类）交互式图形界面，
#         提供表单验证、智能默认填充、原子写入 JSON 与 .env 同步更新
# - 输出：ConfigGuiApp 实例
# =========================================================================
class ConfigGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MCPHub 智能配置向导 (Hermes Desktop 增强)")
        self.root.geometry("640x760")
        self.root.minsize(580, 700)

        # 扫描现有分类数据
        self.capabilities_map = scan_existing_capabilities()

        # 配置全局字体与视觉风格
        self._setup_styles()
        # 构建页面布局
        self._create_widgets()
        # 初始化默认选中状态
        self._on_category_mode_change()

    def _setup_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 9))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 13, "bold"), foreground="#1e293b")
        style.configure("SubHeader.TLabel", font=("Microsoft YaHei UI", 9), foreground="#64748b")
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"), foreground="#0f172a")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=6)

    def _create_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding="16 16 16 16")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. 顶部标题栏
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(header_frame, text="✨ MCPHub 引擎与自定义能力配置向导", style="Header.TLabel").pack(anchor=tk.W)
        ttk.Label(
            header_frame,
            text="无需手动编辑代码或 JSON 文件，在界面中快捷配置引擎参数，一键安全生效。",
            style="SubHeader.TLabel"
        ).pack(anchor=tk.W, pady=(2, 0))

        # 2. 模块一：选择或新建能力分类
        cat_group = ttk.LabelFrame(main_frame, text=" 第一步：能力分类与归属 ", style="Section.TLabelframe", padding="12 10")
        cat_group.pack(fill=tk.X, pady=(0, 10))

        self.mode_var = tk.StringVar(value="existing")
        cat_mode_frame = ttk.Frame(cat_group)
        cat_mode_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Radiobutton(
            cat_mode_frame,
            text="加入已有能力分类（例如通用搜索或网页提取）",
            variable=self.mode_var,
            value="existing",
            command=self._on_category_mode_change
        ).pack(anchor=tk.W, pady=2)

        ttk.Radiobutton(
            cat_mode_frame,
            text="创建全新自定义能力分类（例如 academic_search、code_search 等）",
            variable=self.mode_var,
            value="new",
            command=self._on_category_mode_change
        ).pack(anchor=tk.W, pady=2)

        # 已有分类下拉选择
        self.existing_cat_frame = ttk.Frame(cat_group)
        self.existing_cat_frame.pack(fill=tk.X, pady=4)
        ttk.Label(self.existing_cat_frame, text="目标分类：", width=12).pack(side=tk.LEFT)
        existing_options = list(self.capabilities_map.keys())
        if "web_search" in existing_options:
            default_cat = "web_search"
        elif existing_options:
            default_cat = existing_options[0]
        else:
            default_cat = "web_search"
        self.existing_cat_var = tk.StringVar(value=default_cat)
        self.existing_cat_cb = ttk.Combobox(
            self.existing_cat_frame,
            textvariable=self.existing_cat_var,
            values=existing_options,
            state="readonly"
        )
        self.existing_cat_cb.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 新建分类输入区域
        self.new_cat_frame = ttk.Frame(cat_group)
        ttk.Label(self.new_cat_frame, text="分类标识：", width=12).grid(row=0, column=0, sticky=tk.W, pady=3)
        self.new_cat_id_var = tk.StringVar(value="academic_search")
        self.new_cat_id_entry = ttk.Entry(self.new_cat_frame, textvariable=self.new_cat_id_var)
        self.new_cat_id_entry.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(self.new_cat_frame, text="（英文小写与下划线，例如 academic_search）", foreground="#64748b").grid(row=0, column=2, sticky=tk.W, padx=6)

        ttk.Label(self.new_cat_frame, text="功能描述：", width=12).grid(row=1, column=0, sticky=tk.W, pady=3)
        self.new_cat_desc_var = tk.StringVar(value="学术文献与论文专业检索")
        self.new_cat_desc_entry = ttk.Entry(self.new_cat_frame, textvariable=self.new_cat_desc_var)
        self.new_cat_desc_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
        self.new_cat_frame.columnconfigure(1, weight=1)

        # 3. 模块二：服务提供商核心参数
        prov_group = ttk.LabelFrame(main_frame, text=" 第二步：服务提供商核心参数 ", style="Section.TLabelframe", padding="12 10")
        prov_group.pack(fill=tk.X, pady=(0, 10))
        prov_group.columnconfigure(1, weight=1)

        # 引擎唯一标识
        ttk.Label(prov_group, text="引擎标识 ID：", width=14).grid(row=0, column=0, sticky=tk.W, pady=4)
        self.prov_id_var = tk.StringVar(value="tavily-search")
        ttk.Entry(prov_group, textvariable=self.prov_id_var).grid(row=0, column=1, sticky="ew", pady=4)

        # 适配器协议类型
        ttk.Label(prov_group, text="适配协议类型：", width=14).grid(row=1, column=0, sticky=tk.W, pady=4)
        self.prov_type_var = tk.StringVar(value="tavily-search")
        adapter_cb = ttk.Combobox(
            prov_group,
            textvariable=self.prov_type_var,
            values=["tavily-search", "exa", "jina", "bocha-search", "anysearch", "context7"],
            state="readonly"
        )
        adapter_cb.grid(row=1, column=1, sticky="ew", pady=4)
        adapter_cb.bind("<<ComboboxSelected>>", self._on_protocol_change)

        # 通信接口 Endpoint
        ttk.Label(prov_group, text="通信接口地址：", width=14).grid(row=2, column=0, sticky=tk.W, pady=4)
        self.endpoint_var = tk.StringVar(value=DEFAULT_ENDPOINTS["tavily-search"])
        ttk.Entry(prov_group, textvariable=self.endpoint_var).grid(row=2, column=1, sticky="ew", pady=4)

        # 优先级与状态设置
        setting_subframe = ttk.Frame(prov_group)
        setting_subframe.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Label(setting_subframe, text="执行优先级：", width=14).pack(side=tk.LEFT)
        self.priority_var = tk.IntVar(value=20)
        ttk.Spinbox(setting_subframe, from_=1, to=1000, textvariable=self.priority_var, width=8).pack(side=tk.LEFT)
        ttk.Label(setting_subframe, text="（1-1000，数值越小越优先调用）", foreground="#64748b").pack(side=tk.LEFT, padx=6)

        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(setting_subframe, text="立即启用此引擎", variable=self.enabled_var).pack(side=tk.RIGHT)

        # 4. 模块三：API 密钥绑定与同步
        key_group = ttk.LabelFrame(main_frame, text=" 第三步：API 密钥绑定与配置（同步到 .env）", style="Section.TLabelframe", padding="12 10")
        key_group.pack(fill=tk.X, pady=(0, 12))
        key_group.columnconfigure(1, weight=1)

        ttk.Label(key_group, text="变量名称：", width=14).grid(row=0, column=0, sticky=tk.W, pady=4)
        self.env_name_var = tk.StringVar(value="TAVILY_API_KEY")
        ttk.Entry(key_group, textvariable=self.env_name_var).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(key_group, text="API Key 密钥：", width=14).grid(row=1, column=0, sticky=tk.W, pady=4)
        self.api_key_var = tk.StringVar()
        self.api_key_entry = ttk.Entry(key_group, textvariable=self.api_key_var, show="*")
        self.api_key_entry.grid(row=1, column=1, sticky="ew", pady=4)

        show_key_frame = ttk.Frame(key_group)
        show_key_frame.grid(row=2, column=1, sticky="w", pady=(2, 4))
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            show_key_frame,
            text="显示明文密钥",
            variable=self.show_key_var,
            command=self._toggle_show_key
        ).pack(side=tk.LEFT)
        ttk.Label(show_key_frame, text="（若已在 .env 配置可留空；填写将自动更新 .env）", foreground="#64748b").pack(side=tk.LEFT, padx=6)

        # 5. 底部按钮控制栏
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(btn_frame, text="💾 保存并应用配置", style="Primary.TButton", command=self._save_configuration).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="🔄 重置表单", command=self._reset_form).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="关闭退出", command=self.root.destroy).pack(side=tk.LEFT, padx=4)

    def _on_category_mode_change(self) -> None:
        mode = self.mode_var.get()
        if mode == "existing":
            self.new_cat_frame.pack_forget()
            self.existing_cat_frame.pack(fill=tk.X, pady=4)
        else:
            self.existing_cat_frame.pack_forget()
            self.new_cat_frame.pack(fill=tk.X, pady=4)

    def _on_protocol_change(self, event: Any = None) -> None:
        proto = self.prov_type_var.get()
        if proto in DEFAULT_ENDPOINTS:
            self.endpoint_var.set(DEFAULT_ENDPOINTS[proto])
        # 建议自动生成变量名
        clean_name = proto.replace("-search", "").upper()
        self.env_name_var.set(f"{clean_name}_API_KEY")

    def _toggle_show_key(self) -> None:
        if self.show_key_var.get():
            self.api_key_entry.config(show="")
        else:
            self.api_key_entry.config(show="*")

    def _reset_form(self) -> None:
        self.prov_id_var.set("tavily-search")
        self.prov_type_var.set("tavily-search")
        self.endpoint_var.set(DEFAULT_ENDPOINTS["tavily-search"])
        self.priority_var.set(20)
        self.enabled_var.set(True)
        self.env_name_var.set("TAVILY_API_KEY")
        self.api_key_var.set("")

    def _save_configuration(self) -> None:
        mode = self.mode_var.get()
        prov_id = self.prov_id_var.get().strip()
        prov_type = self.prov_type_var.get().strip()
        endpoint = self.endpoint_var.get().strip()
        # 缺失读取修复：这两个控件值此前从未取出，导致下方 NameError
        env_key_name = self.env_name_var.get().strip()
        enabled = bool(self.enabled_var.get())

        # 1. 字段基础校验与异常防御
        if not prov_id or not re.match(r"^[a-zA-Z0-9_\-]+$", prov_id):
            messagebox.showerror("输入错误", "引擎标识 ID 不能为空，且只能包含字母、数字、下划线与短横线！")
            return

        try:
            priority = int(self.priority_var.get())
            if not 1 <= priority <= 1000:
                raise ValueError()
        except (tk.TclError, ValueError):
            messagebox.showerror("输入错误", "执行优先级必须是 1 到 1000 之间的整数（数值越小越优先调用）！")
            return

        if not endpoint.startswith("https://"):
            messagebox.showerror("安全错误", "通信接口地址必须以 https:// 开头以确保传输加密与安全合规！")
            return

        try:
            from urllib.parse import urlsplit
            import ipaddress
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                messagebox.showerror("安全错误", "通信接口地址格式不规范，且禁止携带内嵌用户名或密码凭据！")
                return
            if parsed.port not in (None, 443):
                messagebox.showerror("安全错误", "通信接口仅允许标准 HTTPS 端口（443 或缺省端口），禁止指向非标探测端口！")
                return
            hostname = parsed.hostname.lower()
            if hostname in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
                messagebox.showerror("安全错误", "通信接口禁止指向本机回环或局域网私网地址 (localhost)！")
                return
            try:
                ip = ipaddress.ip_address(hostname)
                if not ip.is_global:
                    messagebox.showerror("安全错误", f"通信接口禁止指向私网或局域网保留 IP 地址 ({hostname})！")
                    return
            except ValueError:
                pass
        except Exception as exc:
            messagebox.showerror("格式错误", f"解析通信接口地址失败：{exc}")
            return

        if not env_key_name or not re.match(r"^[A-Z_][A-Z0-9_]*$", env_key_name):
            messagebox.showerror("输入错误", "环境变量名只能由大写英文字母、数字和下划线组成，且不能以数字开头（例如 TAVILY_API_KEY）！")
            return

        api_key_val = self.api_key_var.get().strip().strip("'\"")

        # 2. 确定目标能力分类名称与保存目标文件
        if mode == "existing":
            cap_name = self.existing_cat_var.get().strip()
            if not cap_name or cap_name not in self.capabilities_map:
                messagebox.showerror("错误", "请选择有效的已有能力分类！若尚无分类，请选择『创建全新自定义能力分类』。")
                return
            target_file, cap_def = self.capabilities_map[cap_name]
        else:
            cap_name = self.new_cat_id_var.get().strip().lower()
            if not cap_name or not re.match(r"^[a-zA-Z0-9_]+$", cap_name):
                messagebox.showerror("输入错误", "新分类标识不能为空，且只能包含小写英文字母、数字和下划线（例如 academic_search）！")
                return
            if cap_name in self.capabilities_map:
                messagebox.showerror("错误", f"能力分类 [{cap_name}] 已经存在！若要添加引擎请选择『加入已有分类』。")
                return
            target_file = PROVIDERS_DIR / f"{cap_name}.json"
            cap_def = {
                "description": self.new_cat_desc_var.get().strip() or f"Custom {cap_name} capability",
                "timeout_seconds": 25,
                "cooldown_seconds": 3600,
                "quota_cooldown_seconds": 259200,
                "providers": []
            }

        # 构造待写入的引擎结构
        new_provider_entry: dict[str, Any] = {
            "id": prov_id,
            "type": prov_type,
            "enabled": enabled,
            "priority": priority,
            "api_key_env": env_key_name,
            "endpoint": endpoint
        }

        # 读取并更新目标 JSON 文件
        try:
            PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
            if target_file.exists():
                full_doc = json.loads(target_file.read_text(encoding="utf-8"))
            else:
                full_doc = {"capabilities": {}}

            if "capabilities" not in full_doc or not isinstance(full_doc["capabilities"], dict):
                full_doc["capabilities"] = {}

            if cap_name not in full_doc["capabilities"]:
                full_doc["capabilities"][cap_name] = cap_def

            providers_list = full_doc["capabilities"][cap_name].setdefault("providers", [])
            # 检查引擎 ID 是否已存在
            existing_idx = next((i for i, p in enumerate(providers_list) if isinstance(p, dict) and p.get("id") == prov_id), None)
            if existing_idx is not None:
                if not messagebox.askyesno("确认覆盖", f"能力 [{cap_name}] 中已存在引擎 ID [{prov_id}]，是否覆盖其配置？"):
                    return
                providers_list[existing_idx] = new_provider_entry
            else:
                providers_list.append(new_provider_entry)

            # 按 priority 升序重新排序，保持文件规范整洁
            providers_list.sort(key=lambda item: item.get("priority", 1000))

            # 写入更新后的 JSON 文件
            target_file.write_text(json.dumps(full_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

            # 同步保存 API Key 到 .env 文件
            if api_key_val:
                save_api_key_to_dotenv(env_key_name, api_key_val)

        except Exception as exc:
            messagebox.showerror("保存失败", f"写入配置文件时发生异常：\n{exc}")
            return

        # 刷新本界面的分类映射
        self.capabilities_map = scan_existing_capabilities()
        self.existing_cat_cb.config(values=list(self.capabilities_map.keys()))

        success_msg = (
            f"🎉 配置已成功保存！\n\n"
            f"1. 配置文件已安全更新：\n   {target_file.name}\n"
            f"2. 挂载能力分类：{cap_name}\n"
            f"3. 注册引擎标识：{prov_id} (优先级: {priority})\n"
        )
        if api_key_val:
            success_msg += f"4. API Key 变量 [{env_key_name}] 已成功同步到 .env\n"
        success_msg += "\n💡 提示：在 Hermes Desktop 客户端中新建会话或重新启动后，将自动识别最新能力与引擎！"

        messagebox.showinfo("配置成功", success_msg)


# =========================================================================
# 【功能块：程序独立运行主入口】
# - 输入：无
# - 功能：初始化 Tk 窗口循环，捕捉未捕获异常并友好弹出提示框
# - 输出：无
# =========================================================================
def main() -> None:
    root = tk.Tk()
    app = ConfigGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
