export const PAGE_LAYOUTS = {
  'a4-1': {
    pageWidth: 794, pageHeight: 1122, pageGap: 28,
    top: 95, bottom: 95, left: 95, right: 95, columns: 1, columnGap: 0,
  },
  'a4-2': {
    pageWidth: 794, pageHeight: 1122, pageGap: 28,
    top: 59, bottom: 55, left: 51, right: 51, columns: 2, columnGap: 28,
  },
  slides: {
    pageWidth: 960, pageHeight: 540, pageGap: 28,
    top: 33, bottom: 30, left: 38, right: 38, columns: 1, columnGap: 0,
  },
};

/**
 * 把 ProseMirror 的顶层块放入固定页面/栏位。这里只计算视觉位置，不写回自动分页；
 * 显式分页块会无条件进入下一页，普通块则尽量保持完整，过高块才允许越过页底。
 */
export function layoutPaginatedBlocks(blocks, layoutName = 'a4-1') {
  const spec = PAGE_LAYOUTS[layoutName] || PAGE_LAYOUTS['a4-1'];
  const contentWidth = spec.pageWidth - spec.left - spec.right;
  const columnWidth = (contentWidth - spec.columnGap * (spec.columns - 1)) / spec.columns;
  const contentHeight = spec.pageHeight - spec.top - spec.bottom;
  const pitch = spec.pageHeight + spec.pageGap;
  const placements = [];
  let page = 0;
  let column = 0;
  let cursor = spec.top;
  let maxBottom = spec.pageHeight;
  let slotHasContent = false;

  const advanceSlot = () => {
    if (column + 1 < spec.columns) column += 1;
    else { page += 1; column = 0; }
    cursor = page * pitch + spec.top;
    slotHasContent = false;
  };

  let seenPracticeSolve = false;

  blocks.forEach(block => {
    const marginTop = Math.max(0, Number(block.marginTop) || 0);
    const marginBottom = Math.max(0, Number(block.marginBottom) || 0);
    const height = Math.max(0, Number(block.height) || 0);
    if (block.kind === 'pageBreak') {
      placements.push({
        x: spec.left,
        y: page * pitch + spec.pageHeight + Math.floor(spec.pageGap / 2),
        width: contentWidth,
        page,
        column,
        pageBreak: true,
      });
      page += 1;
      column = 0;
      cursor = page * pitch + spec.top;
      slotHasContent = false;
      maxBottom = Math.max(maxBottom, page * pitch + spec.pageHeight);
      return;
    }

    // 与最终 TeX 双栏规则一致：第一道大题若当前栏能完整放下就紧跟小题，否则
    // 换栏；第二道起每题强制新栏。这里只做编辑画布分页提示，最终 PDF 仍由 TeX
    // 对公式、图片和作答区做真实盒高测量。
    if (spec.columns === 2 && block.practiceSolve) {
      // 显式分页已经进入一张全新的左栏时，不再多跳一次到右栏。
      if (seenPracticeSolve && slotHasContent) advanceSlot();
      seenPracticeSolve = true;
    }

    const slotTop = page * pitch + spec.top;
    const slotBottom = page * pitch + spec.pageHeight - spec.bottom;
    const required = marginTop + height + marginBottom;
    if (cursor > slotTop && cursor + required > slotBottom && required <= contentHeight) {
      advanceSlot();
    }
    const x = spec.left + column * (columnWidth + spec.columnGap);
    const y = cursor + marginTop;
    placements.push({x, y, width: columnWidth, page, column, pageBreak: false});
    cursor = y + height + marginBottom;
    slotHasContent = true;
    maxBottom = Math.max(maxBottom, cursor + spec.bottom);
  });

  const pageCount = Math.max(1, Math.floor((maxBottom - 1) / pitch) + 1, page + 1);
  return {
    placements,
    pageCount,
    paperHeight: pageCount * pitch - spec.pageGap,
    ...spec,
    columnWidth,
    contentWidth,
  };
}
