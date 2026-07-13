from config import _IS_LINUX

if _IS_LINUX:
    from ui_automation.linux_navigator import ui_navigator
else:
    from ui_automation.windows_uia import ui_navigator