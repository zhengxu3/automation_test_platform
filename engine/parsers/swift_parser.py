"""
Swift AST 解析器 — 提取类/方法/调用关系/UI绑定
输出格式与 jvm_jar 一致，复用向量检索和爆炸范围分析
"""
import os
import re
from typing import List, Dict


class SwiftASTParser:
    """解析 Swift 项目，提取方法级 AST 节点 + UI 绑定关系"""

    SKIP_DIRS = {"Pods", "build", ".build", "DerivedData", "Carthage", "Tests", "UITests"}
    EXTENSIONS = {".swift"}

    def __init__(self):
        # Storyboard/XIB 中的 action 绑定: selector -> (storyboard_file, control_id)
        self._storyboard_actions = {}
        # Storyboard/XIB 中的 outlet 绑定: property -> (storyboard_file, control_id)
        self._storyboard_outlets = {}

    async def parse(self, repo_path: str, language: str, target_file: str = None) -> List[Dict]:
        """解析整个仓库或单个文件"""
        # 1. 先解析所有 Storyboard/XIB 的 UI 绑定
        self._parse_storyboards(repo_path)

        # 2. 解析 Swift 文件
        nodes = []
        if target_file:
            files = [target_file]
        else:
            files = self._collect_swift_files(repo_path)

        for filepath in files:
            file_nodes = self._parse_file(filepath, repo_path)
            nodes.extend(file_nodes)

        return nodes

    def _collect_swift_files(self, repo_path: str) -> List[str]:
        """收集所有 Swift 文件"""
        files = []
        for root, dirs, filenames in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS and not d.startswith('.')]
            for fname in filenames:
                if os.path.splitext(fname)[1] in self.EXTENSIONS:
                    files.append(os.path.join(root, fname))
        return files

    def _parse_storyboards(self, repo_path: str):
        """解析 Storyboard/XIB 文件，提取 action 和 outlet 绑定"""
        for root, dirs, filenames in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS and not d.startswith('.')]
            for fname in filenames:
                if fname.endswith(('.storyboard', '.xib')):
                    self._parse_single_storyboard(os.path.join(root, fname), fname)

    def _parse_single_storyboard(self, filepath: str, filename: str):
        """解析单个 Storyboard/XIB"""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # 提取 action: <action selector="closeBtnTapped:" destination="..." eventType="touchUpInside" id="..."/>
            for m in re.finditer(r'<action\s+selector="([^"]+)"[^>]*id="([^"]+)"', content):
                selector = m.group(1).rstrip(':')
                control_id = m.group(2)
                self._storyboard_actions[selector] = (filename, control_id)

            # 提取 outlet: <outlet property="closeBtn" destination="..." id="..."/>
            for m in re.finditer(r'<outlet\s+property="([^"]+)"\s+destination="([^"]+)"', content):
                prop = m.group(1)
                dest_id = m.group(2)
                self._storyboard_outlets[prop] = (filename, dest_id)

        except Exception:
            pass

    def _parse_file(self, filepath: str, repo_path: str) -> List[Dict]:
        """解析单个 Swift 文件"""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception:
            return []

        rel_path = filepath.replace(repo_path, '')
        lines = content.split('\n')
        nodes = []

        # 提取当前类名
        current_class = self._extract_class_name(content, filepath)

        # 提取所有 IBOutlet 属性 -> 用于 UI 绑定追踪
        iboutlets = {}  # property_name -> type
        for m in re.finditer(r'@IBOutlet\s+(?:weak\s+)?var\s+(\w+)\s*:\s*(\w+)', content):
            iboutlets[m.group(1)] = m.group(2)

        # 提取代码创建的 UI 控件（lazy var / let / var + UI类型）
        ui_types = {'UIButton', 'UILabel', 'UIImageView', 'UIView', 'UITextField', 'UITextView',
                    'UISwitch', 'UISlider', 'UITableView', 'UICollectionView', 'UIScrollView',
                    'PressableButton', 'ShrinkButton', 'UIStackView', 'UISegmentedControl'}
        for m in re.finditer(r'(?:private\s+|fileprivate\s+)?(?:lazy\s+)?(?:let|var)\s+(\w+)\s*(?::\s*(\w+)|=\s*(\w+)\()', content):
            name = m.group(1)
            type_name = m.group(2) or m.group(3) or ''
            if type_name in ui_types or 'Button' in type_name or 'Btn' in name:
                iboutlets[name] = type_name or 'UIControl'

        # 提取方法
        method_pattern = re.compile(
            r'^(\s*)'  # indentation
            r'(?:@IBAction\s+|@objc\s+|override\s+|private\s+|fileprivate\s+|internal\s+|public\s+|open\s+|static\s+|class\s+)*'
            r'func\s+(\w+)\s*\(([^)]*)\)',
            re.MULTILINE
        )

        for m in method_pattern.finditer(content):
            method_name = m.group(2)
            params = m.group(3)
            start_pos = m.start()
            line_num = content[:start_pos].count('\n') + 1

            # 提取方法体
            body_start = content.find('{', m.end())
            if body_start == -1:
                continue
            body = self._extract_body(content, body_start)
            if not body:
                continue

            # 提取调用关系
            calls_out = self._extract_calls(body)

            # 提取 UI 绑定
            ui_bindings = self._extract_ui_bindings(method_name, body, iboutlets)

            # 检查是否是 IBAction
            line_content = lines[line_num - 1] if line_num <= len(lines) else ''
            is_ibaction = '@IBAction' in content[max(0, start_pos - 50):start_pos + 10]

            if is_ibaction:
                # IBAction 方法本身就是 UI 入口
                sb_info = self._storyboard_actions.get(method_name)
                if sb_info:
                    ui_bindings.append(f"{sb_info[0]}::{method_name}")
                else:
                    ui_bindings.append(f"IBAction::{method_name}")

            node_id = f"{rel_path}::{current_class}::{method_name}#L{line_num}"
            nodes.append({
                "node_id": node_id,
                "file_path": rel_path,
                "node_type": "method",
                "class_name": current_class,
                "method_name": method_name,
                "params": params.strip(),
                "code_content": body[:8000],
                "calls_out": calls_out,
                "ui_bindings": ui_bindings,
                "docstring": "",
                "line_number": line_num,
            })

        return nodes

    def _extract_class_name(self, content: str, filepath: str) -> str:
        """提取主类名"""
        # 优先匹配 class/struct/enum 声明
        m = re.search(r'(?:final\s+)?(?:class|struct|enum)\s+(\w+)', content)
        if m:
            return m.group(1)
        # fallback: 用文件名
        return os.path.splitext(os.path.basename(filepath))[0]

    def _extract_body(self, content: str, brace_start: int) -> str:
        """提取花括号内的方法体"""
        depth = 0
        i = brace_start
        while i < len(content):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    return content[brace_start:i + 1]
            i += 1
        return content[brace_start:min(brace_start + 2000, len(content))]

    def _extract_calls(self, body: str) -> List[str]:
        """从方法体中提取函数调用"""
        calls = set()

        # 匹配方法调用: xxx.method( 或 method(
        for m in re.finditer(r'(?:\w+\.)?(\w+)\s*\(', body):
            name = m.group(1)
            # 过滤关键字和常见非方法名
            if name in ('if', 'guard', 'switch', 'while', 'for', 'return', 'let', 'var', 'print', 'self', 'super', 'init', 'deinit'):
                continue
            if name[0].isupper() and not body[m.start():m.start()+50].count('.'):
                # 可能是构造函数调用，也记录
                pass
            calls.add(name)

        # 匹配 #selector(xxx)
        for m in re.finditer(r'#selector\((?:self\.)?(\w+)', body):
            calls.add(m.group(1))

        # 匹配 perform(#selector(self.xxx))
        for m in re.finditer(r'#selector\((?:\w+\.)?(\w+)\)', body):
            calls.add(m.group(1))

        return list(calls)[:50]  # 限制数量

    def _extract_ui_bindings(self, method_name: str, body: str, iboutlets: dict) -> List[str]:
        """提取方法中的 UI 绑定关系"""
        bindings = []

        # 1. addTarget 绑定: xxx.addTarget(self, action: #selector(yyy), for: .touchUpInside)
        for m in re.finditer(r'(\w+)\.addTarget\([^,]+,\s*action:\s*#selector\((?:self\.)?(\w+)\)', body):
            control = m.group(1)
            target_method = m.group(2)
            # 查找 control 对应的 outlet
            outlet_info = self._storyboard_outlets.get(control)
            if outlet_info:
                bindings.append(f"{outlet_info[0]}::{control}")
            else:
                # 用 IBOutlet 类型推断
                control_type = iboutlets.get(control, 'unknown')
                bindings.append(f"code::{control}({control_type})")

        # 2. 手势绑定: UITapGestureRecognizer(target: self, action: #selector(xxx))
        for m in re.finditer(r'(\w+GestureRecognizer)\(target:\s*self,\s*action:\s*#selector\((?:self\.)?(\w+)\)', body):
            gesture_type = m.group(1)
            bindings.append(f"gesture::{gesture_type}")

        # 3. 方法体中直接操作的 IBOutlet 控件
        for outlet_name, outlet_type in iboutlets.items():
            if re.search(rf'\b{outlet_name}\b', body):
                outlet_info = self._storyboard_outlets.get(outlet_name)
                if outlet_info:
                    bindings.append(f"{outlet_info[0]}::{outlet_name}")
                else:
                    bindings.append(f"outlet::{outlet_name}({outlet_type})")

        return bindings


# ==================== 测试 ====================
if __name__ == "__main__":
    import asyncio
    import json

    async def test():
        parser = SwiftASTParser()
        repo_path = os.path.expanduser("~/Documents/work_code/telebird-ios")
        nodes = await parser.parse(repo_path, "ios")

        print(f"总节点数: {len(nodes)}")
        print(f"有 ui_bindings 的: {sum(1 for n in nodes if n['ui_bindings'])}")
        print(f"有 calls_out 的: {sum(1 for n in nodes if n['calls_out'])}")

        # 打印几个有 UI 绑定的节点
        print("\n=== 有 UI 绑定的节点示例 ===")
        ui_nodes = [n for n in nodes if n['ui_bindings']]
        for n in ui_nodes[:5]:
            print(json.dumps({
                "node_id": n["node_id"],
                "calls_out": n["calls_out"][:5],
                "ui_bindings": n["ui_bindings"]
            }, ensure_ascii=False, indent=2))

    asyncio.run(test())
