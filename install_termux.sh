#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_PY="$SCRIPT_DIR/dy_dl.py"

if [ ! -f "$SRC_PY" ]; then
  echo "❌ 未找到 $SRC_PY，请把 dy_dl.py 与本安装脚本放在同一目录后重试。" >&2
  exit 1
fi

mkdir -p "$HOME/bin" "$HOME/.shortcuts"

cp "$SRC_PY" "$HOME/bin/dy_dl.py"
chmod +x "$HOME/bin/dy_dl.py"

write_wrapper() {
  local target="$1"
  local body="$2"
  cat > "$target" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
$body
EOF
  chmod +x "$target"
}

write_wrapper "$HOME/bin/dy-dl" 'exec python3 "$HOME/bin/dy_dl.py" "$@"'
write_wrapper "$HOME/bin/termux-url-opener" 'exec python3 "$HOME/bin/dy_dl.py" --pause-on-error "$@"'
write_wrapper "$HOME/bin/termux-file-editor" 'exec python3 "$HOME/bin/dy_dl.py" --pause-on-error "$@"'
write_wrapper "$HOME/.shortcuts/抖音下载" 'exec python3 "$HOME/bin/dy_dl.py" --pause-on-error'
write_wrapper "$HOME/.shortcuts/抖音下音频" 'exec python3 "$HOME/bin/dy_dl.py" --audio-only --pause-on-error'
write_wrapper "$HOME/.shortcuts/抖音提字幕" 'exec python3 "$HOME/bin/dy_dl.py" --subtitle-only --pause-on-error'
write_wrapper "$HOME/.shortcuts/抖音下视频+字幕" 'exec python3 "$HOME/bin/dy_dl.py" --transcribe --pause-on-error'

HAS_UPDATE_CMD=0
if [ -d "$SCRIPT_DIR/.git" ]; then
  ESCAPED_DIR="$(printf '%q' "$SCRIPT_DIR")"
  write_wrapper "$HOME/bin/dy-update" "git -C $ESCAPED_DIR pull --ff-only && exec bash $ESCAPED_DIR/install_termux.sh"
  HAS_UPDATE_CMD=1
fi

BASHRC="$HOME/.bashrc"
touch "$BASHRC"

ALIAS_DY="alias dy='python3 \"\$HOME/bin/dy_dl.py\"'"
ALIAS_DYA="alias dya='python3 \"\$HOME/bin/dy_dl.py\" --audio-only'"
ALIAS_DYS="alias dys='python3 \"\$HOME/bin/dy_dl.py\" --subtitle-only'"
ALIAS_DYT="alias dyt='python3 \"\$HOME/bin/dy_dl.py\" --transcribe'"
ALIAS_UPDATE="alias dy-update='bash \"\$HOME/bin/dy-update\"'"

for alias_line in "$ALIAS_DY" "$ALIAS_DYA" "$ALIAS_DYS" "$ALIAS_DYT"; do
  if ! grep -qxF "$alias_line" "$BASHRC"; then
    printf '%s\n' "$alias_line" >> "$BASHRC"
  fi
done
if [ "$HAS_UPDATE_CMD" -eq 1 ] && ! grep -qxF "$ALIAS_UPDATE" "$BASHRC"; then
  printf '%s\n' "$ALIAS_UPDATE" >> "$BASHRC"
fi

echo "✅ 安装完成！已部署以下入口："
echo "   - 主脚本：$HOME/bin/dy_dl.py"
echo "   - 分享接管：$HOME/bin/termux-url-opener / $HOME/bin/termux-file-editor"
echo "   - 桌面快捷方式：$HOME/.shortcuts/抖音下载 、抖音下音频 、抖音提字幕 、抖音下视频+字幕"
echo "   - 终端命令：dy（下载）、dya（下音频）、dys（仅提字幕）、dyt（视频+字幕）"
if [ "$HAS_UPDATE_CMD" -eq 1 ]; then
  echo "   - 一键更新：dy-update（从 Git 拉取最新代码并更新本地脚本）"
fi
echo "     （新开终端生效，或执行 source ~/.bashrc）"
echo ""
echo "ℹ️ 若尚未初始化环境，请在 Termux 执行："
echo "   pkg install -y python termux-api ffmpeg"
echo "   termux-setup-storage"
echo ""
echo "🎙️ 如需使用云端转录字幕（默认硅基流动 SenseVoiceSmall 免费接口）："
echo '   dy --set-key "sk-你的硅基流动APIKey"'
