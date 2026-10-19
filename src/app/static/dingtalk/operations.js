/**
 * 快捷办理页（operations.html）的页面级逻辑。
 *
 * 通用能力（表单绑定、状态徽标、结果渲染）在 common.js 里，这里只补一层写操作的安全网：
 * 对会立刻送达他人的高风险操作（单聊、群消息、DING、群公告、机器人推送），
 * 勾选「已确认执行」后提交前再弹一次原生确认框，避免误触发出不可撤回的消息。
 */

"use strict";

(function () {
  const DWS = window.DWS;
  if (!DWS) {
    return;
  }

  // 必须早于 initShell 注册，捕获阶段的自定义守卫才能先于通用表单处理执行
  document.querySelectorAll('form[data-action="post"][data-danger="1"]').forEach((form) => {
    form.addEventListener(
      "submit",
      (event) => {
        const confirmBox = form.querySelector('input[name="confirm"]');
        if (!confirmBox || !confirmBox.checked) {
          return;
        }
        const title = (form.querySelector("h3")?.textContent || "该操作").trim();
        const route = form.dataset.path || "";
        const agreed = window.confirm(
          `即将真实执行「${title}」（POST ${route}），内容会立即发送到钉钉，确定继续吗？`,
        );
        if (!agreed) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      },
      true,
    );
  });

  DWS.initShell({ page: "operations" });
})();
