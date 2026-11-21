"""Trusted task context for shared vs private browser sessions, not model claims."""


def instructions(endpoint, *, shared_allowed=False):
    if endpoint.kind=='private':
        scope = '本任务来自私聊；使用本私聊独立 VM 的浏览器，用户明确要求后可登录其个人账号。不能访问、复制或借用其他会话的浏览器资料。'
    elif endpoint.kind=='group' and shared_allowed:
        scope = '宿主已核对本任务请求人是 owner 或本群群主/管理员。仅其明确要求的 bot 公共小号可登录，操作前说明“这个登录全群可用”。群内任何个人账号登录都要拒绝并引导私聊。'
    else:
        scope = '本任务没有群公共账号登录授权。不得新登录账号；个人登录引导私聊，bot 公共小号登录请 owner 或群管理员亲自要求。已有公共登录态可用于用户要求的普通读取；清理公共登录态要请 owner/群管亲自发 /logout，不代替普通成员用脚本绕过。'
    if endpoint.kind=='private':
        credentials = ('用户要求用其本人账号登录、并在本私聊给了手机号、账号密码或短信验证码时，照用来登录；'
                       '这些只用于这次登录，不写进记忆或文件，不复述，不发到别处。')
    else:
        credentials = '群里只支持扫码或手机确认，不索取、猜测、使用密码和短信验证码；有人在群里发了也不用，提醒撤回。'
    return ('\n[浏览器登录边界]\n'+scope+'\n'+credentials+
        '私聊或上述已授权的群公共小号需要人工验证时，请前台用 browser_takeover 发链接到原会话，等用户完成，不代解验证码。群公共小号若只发二维码，只截必要区域，用 gaoji send-file 发回本群，核对原 request_id；登记成功不等于已送达。'
        '用 gaoji_browser.wait_for_login 检查网站实际登录标志，最多三分钟；二维码消失、出现 cookie 或跳转不单独证明登录成功。'
        '被拦截或超时就说明失败，不反复重试。浏览器 cookie/存储值不打印、不导出、不上传宿主，不进入报告或模型上下文。'
        '登录不等于授权付款、发布、删除、授权或提交个人信息，这些仍须用户单独明确要求。\n')
