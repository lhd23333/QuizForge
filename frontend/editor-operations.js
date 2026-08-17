/** 在指定文档位置插入块节点；空段落整块替换，正文中间由 Tiptap 自动拆段。 */
export function insertBlockAt(editor, position, content) {
  const pos = Math.max(0, Math.min(Number(position), editor.state.doc.content.size));
  const $pos = editor.state.doc.resolve(pos);
  if ($pos.parent.type.name === 'paragraph' && $pos.parent.content.size === 0 && $pos.depth > 0) {
    const node = editor.schema.nodeFromJSON(content);
    const transaction = editor.state.tr.replaceWith($pos.before(), $pos.after(), node);
    editor.view.dispatch(transaction);
    return true;
  }
  return editor.commands.insertContentAt(pos, content);
}

/** 按稳定 blockId 把一个题目节点移动到另一个题目节点之前。 */
export function moveQuestionBefore(editor, movingBlockId, targetBlockId) {
  if (!movingBlockId || !targetBlockId || movingBlockId === targetBlockId) return false;
  let sourcePos = -1;
  let targetPos = -1;
  let sourceNode = null;
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name !== 'handoutQuestion') return;
    if (node.attrs.blockId === movingBlockId) {
      sourcePos = pos;
      sourceNode = node;
    }
    if (node.attrs.blockId === targetBlockId) targetPos = pos;
  });
  if (!sourceNode || sourcePos < 0 || targetPos < 0) return false;
  const size = sourceNode.nodeSize;
  let insertPos = targetPos;
  const transaction = editor.state.tr.delete(sourcePos, sourcePos + size);
  if (sourcePos < targetPos) insertPos -= size;
  transaction.insert(insertPos, sourceNode);
  editor.view.dispatch(transaction);
  return true;
}
