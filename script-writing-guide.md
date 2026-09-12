# KodakkuAssist 脚本编写指南

当单纯的条件判断不够用、需要更复杂的逻辑时，用脚本实现。脚本是触发器功能的完全上位替代，简单功能也推荐用脚本写，方便后续扩展。脚本用 [C#](https://learn.microsoft.com/zh-cn/dotnet/csharp/) 编写，需要最基础的 C# 知识。

完整可运行的示例见仓库根目录的 [SimpleScript.cs](./SimpleScript.cs)；更复杂的实战写法见 `Karlin-Z/` 下的脚本。

## 最小结构

一个脚本由三部分组成：引用命名空间、一个带 `[ScriptType]` 的 `public` 类（非抽象、有公开无参构造函数）、类里的属性与方法。

```cs
using System;
using KodakkuAssist.Script;              // ScriptType / ScriptMethod / UserSetting / ScriptData / ScriptAccessory
using KodakkuAssist.Module.GameEvent;    // Event / EventTypeEnum
using KodakkuAssist.Module.Draw;         // DrawModeEnum / DrawTypeEnum

[ScriptType(name: "示例脚本", territorys: [179, 979],
            guid: "d3b6a9b4-1e0e-4e0c-b7c7-ff1fce0e6cf2",
            version: "0.0.0.3", author: "Karlin")]
public class MyScript
{
    int count = 0;                       // 运行时状态，脚本重载即重置

    public void Init(ScriptAccessory accessory) => count = 0;

    [ScriptMethod(name: "医济计数", eventType: EventTypeEnum.StartCasting,
                  eventCondition: ["ActionId:133"])]
    public void OnMedica(Event @event, ScriptAccessory accessory)
    {
        count++;
        accessory.Method.SendChat($"第 {count} 次医济");
    }
}
```

- `[ScriptMethod]` 方法必须 `public`，签名固定为 `(Event, ScriptAccessory)`；方法名随意，用户看到的是 `name`。
- 每条游戏事件都会匹配所有方法，`eventType` 一致且全部 `eventCondition` 满足时才执行。
- 方法在线程池上异步执行；多个方法可能并发读写同一字段，注意加锁。
- `Init` 可选，不写则什么都不做；调用时机见文末「生命周期」。

## ScriptType 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `guid` | string | ✅ | — | 唯一标识；guid 相同会被视为同一个脚本 |
| `name` | string | 否 | `Default Script` | 脚本显示名 |
| `territorys` | uint[] | 否 | 空 | 生效的地图 ID；空表示所有区域 |
| `version` | string | 否 | `0.0.0.1` | 版本号，与在线库比较后提示更新 |
| `author` | string | 否 | `Unknown` | 作者，也是在线库文件名的一部分 |
| `note` | string | 否 | 空 | 备注，显示给用户 |
| `updateInfo` | string | 否 | 空 | 更新说明 |

参数可以具名、顺序任意，也可以按上表顺序写位置参数。长文本常用 `const` 引用：

```cs
[ScriptType(name: "绝凯夫卡", territorys: [1363], guid: "...", version: "0.0.0.4", updateInfo: updateInfoStr)]
public class 绝凯夫卡
{
    const string updateInfoStr = """
        精修 P1
        新增分摊击退指示
        """;
}
```

> `territorys` 是 `uint[]` 而不是 `int[]`；留空表示所有区域生效。

## 类属性

| 写法 | 说明 |
| --- | --- |
| `[UserSetting(note)]` | 在插件设置界面生成可编辑项，用户可自定义脚本行为；类型需可被 JSON 序列化（数值 / bool / string / 枚举 / `Vector` / `ScriptColor` 等），`note` 可选 |
| `[ScriptData]` | 不显示在界面，但会跨脚本重载、插件重启保留 |
| 普通字段 | 仅本次运行有效，脚本重载即重置 |

```cs
[UserSetting("爆炸等待时间（毫秒）")]
public int DelayMs { get; set; } = 1000;

[ScriptData]
public Dictionary<uint, int> Counter { get; set; } = new();
```

## ScriptMethod 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `eventType` | `EventTypeEnum` | ✅ | — | 匹配的事件类型 |
| `name` | string | 否 | 方法名 | 用户界面上显示的名称 |
| `eventCondition` | string[] | 否 | 全部匹配 | 形如 `"属性:值"` |
| `userControl` | bool | 否 | `true` | `false` 时用户不可见、不可禁用 |
| `suppress` | uint | 否 | `0` | 节流毫秒数；同一方法在此期间只触发一次 |

### eventCondition 匹配

- `"ActionId:133"`：精确相等。
- `"ActionId:regex:13[34]"`：值以 `regex:` 开头时按正则匹配（不自动加锚点，需完全匹配请写 `^...$`）。
- 值的部分留空（如 `"Targetable:"`）表示该条件不生效。
- 属性名与值都是字符串；事件没有对应属性时视为不匹配。

## Event 对象

```cs
var id = @event["ActionId"];                        // 不存在时返回空字符串
if (@event.TryGet("TargetId", out var tid)) { /* ... */ }
```

- 索引器 `@event["属性名"]` 取值，全部是 `string`；数字请自行 `Convert.ToUInt32(value, 16)` 等。
- 常用成员：`Type`、`ActLog`、`Info`、`DateTime`、`EventTriggerSource`、`PropertiesCopy`。
- 想确认某事件有哪些属性：在插件设置的 GameEvent 页面点开具体日志查看，或临时打印
  `accessory.Log.Debug(string.Join(",", @event.PropertiesCopy.Keys))`。

**通用属性**：多数事件都有 `SourceId` / `SourceName` / `SourceDataId` / `SourcePosition` / `SourceRotation` 及对应的 `Target*`；`*Position` 是 JSON 字符串，形如 `{"X":1.00,"Y":2.00,"Z":3.00}`。下表列出全部 `eventType`，以及各事件在此之外的专有属性：

| 事件 | 说明 | 专有属性 |
| --- | --- | --- |
| `StartCasting` | 开始读条 | `ActionId` `Duration` `DurationMilliseconds` `EffectPosition` |
| `ActionEffect` | 能力技效果（命中 / 伤害） | `ActionId` `EffectPosition` `TargetIndex` |
| `UpdateParty` | 队伍成员更新 | `PartyLength` |
| `Territory` | 切换区域 | `PlaceId` `PlaceName` |
| `AddCombatant` | 单位出现 | `DataId` |
| `RemoveCombatant` | 单位消失 | `DataId` |
| `ChangePlayer` | 主视角玩家切换 | — |
| `PlayerStats` | 玩家属性变化 | — |
| `ChangeMap` | 地图切换 | `MapId` |
| `EffectResult` | 效果结算 | — |
| `StatusAdd` | 获得状态 | `StatusID` `StackCount` `Param` `Duration` `DurationMilliseconds` |
| `StatusRemove` | 失去状态 | `StatusID` `StackCount` `Param` `Duration` `DurationMilliseconds` |
| `StatusList` | 状态列表刷新 | — |
| `CancelAction` | 读条被取消 / 打断 | `ActionId` `Interrupt` |
| `DoT` | 持续伤害跳 | `StatusId` |
| `HoT` | 持续治疗跳 | `StatusId` |
| `Death` | 单位死亡 | — |
| `TargetIcon` | 目标图标 | `Id` |
| `Marker` | 头顶标记 | `Id` `Operate` |
| `Director` | 导演指令 | `Instance` `Command` |
| `Targetable` | 可被选中状态变化 | `Targetable` `DataId` |
| `Tether` | 连线 | `Id` |
| `SystemLog` | 系统日志 | — |
| `MorelogCompat` | morelog 兼容事件 | `MorlogId` `Id` |
| `Waymark` | 地面标点 | `Id` `Operate` `Position` |
| `Gauge` | 副本资源量 | — |
| `UpdateHpMp` | 血量 / 魔力更新 | `Hp` `Mp` `Cp` `Gp` |
| `NpcYell` | NPC 喊话 | `Id` |
| `ObjectEffect` | 场地 / 单位特效 | `Id1` `Id2` |
| `CombatChanged` | 战斗状态变化 | `Type` `InCombat` `Command` |
| `EnvControl` | 环境控制（mapEffect） | `DirectorId` `State` `Id` `Index` `Parm4` |
| `SetObjPos` | 对象位置变化 | `MorlogId` `Id` |
| `Chat` | 聊天消息 | `Sender` `Message` `Type` |
| `PlayActionTimeline` | 动作演出 | `Id` |
| `Countdown` | 倒计时 | `Type` `Duration` `DurationMilliseconds` |
| `KnockBack` | 击退 | `Distance` `Duration` `DurationMilliseconds` `Rotation` |
| `ObjectChanged` | 对象变化 | `Operate` `DataId` `Kind` |
| `ObjectVfx` | 对象特效 | `Id` |
| `VfxEvent` | VFX 事件 | `Id` `Handle` `Type` `Rotation` `Scale` `Speed` `EffectPosition` |

## ScriptAccessory

`accessory` 分三块：`Data`（数据）、`Method`（操作）、`Log`（日志）。

### accessory.Data

| 成员 | 说明 |
| --- | --- |
| `Me` | 主视角玩家 ID（录像回放时为回放主视角） |
| `MyObject` | 主视角玩家对象 |
| `Objects` | GameObject 对象表 |
| `PartyList` | 队伍成员 ID（`List<uint>`） |
| `EnmityList` | 仇恨表 `EnemyId → List<PlayerId>` |
| `DefaultDangerColor` / `DefaultSafeColor` | 默认危险 / 安全颜色（`Vector4`） |
| `GetDefaultDrawProperties()` | 创建一份绘制参数 |

### accessory.Method

| 方法 | 说明 |
| --- | --- |
| `SendChat(text)` | 发送聊天，`\n` 换行 |
| `TextInfo(text, durationMs, isWarning)` | 屏幕中央横幅 |
| `SendDraw(mode, type, props[, callback])` | 发送绘图，`callback` 为每帧回调 |
| `RemoveDraw(nameRegex)` | 按名称正则删除绘图 |
| `UseAction(targetId, actionId[, actionType])` | 对目标使用技能 |
| `UseActionLocation(pos / targetId, actionId)` | 使用地面技能 |
| `TTS(text[, rate])` | 语音朗读 |
| `SelectTarget(targetId)` | 选中目标 |
| `Mark(actorId, markType[, local])` / `MarkClear()` | 设置 / 清除头顶标记 |
| `HttpGet(url)` / `HttpPost(url, data)` | HTTP 请求 |
| `RunOnMainThreadAsync(action)` | 在主线程执行 |
| `RegistFrameworkUpdateAction(action[, onMainThread])` | 注册每帧更新，返回 guid |
| `UnregistFrameworkUpdateAction(guid)` / `ClearFrameworkUpdateAction(this)` | 注销 |
| `VfxMethod.*` | 创建 / 控制 VFX（Omen、LockOn、Channeling） |
| `ObjectMethod.*` | 创建 / 销毁临时对象 |

> `Mark` 的 `markType` 取值：`Attack1`~`Attack8`、`Bind1`~`Bind3`、`Stop1` `Stop2`、`Square` `Circle` `Cross` `Triangle`、`None`。

### accessory.Log

`Debug(text)` / `Error(text)`，输出到插件日志。

## 绘图

```cs
var prop = accessory.Data.GetDefaultDrawProperties();
prop.Owner = Convert.ToUInt32(@event["SourceId"], 16); // 绑定到对象
prop.Scale = new(5);
prop.Color = accessory.Data.DefaultDangerColor;
prop.DestoryAt = 5000;                                  // 毫秒
accessory.Method.SendDraw(DrawModeEnum.Default, DrawTypeEnum.Circle, prop);
```

- `DrawModeEnum`：`Default`（跟随用户设置）、`Imgui`、`Vfx`。
- `DrawTypeEnum`：`Circle`、`Donut`、`Fan`、`Rect`、`Straight`、`Line`、`HotWing`、`Arrow`、`SightAvoid`、`Displacement`。

常用绘制参数：

| 参数 | 说明 |
| --- | --- |
| `Owner` / `TargetObject` / `TargetObjectId` | 绑定对象 |
| `Position` / `TargetPosition` | 固定坐标 |
| `Rotation` / `FixRotation` | 朝向 |
| `Scale` / `InnerScale` | 大小 / 月环内径 |
| `Radian` | 扇形角度 |
| `Color` / `TargetColor` | 颜色 |
| `CentreResolvePattern` / `TargetResolvePattern` | 绑定目标的解析方式：`Normal` `OwnerTarget` `OwnerEnmityOrder` `PlayerNearestOrder` `PlayerFarestOrder` `TetherSource` `TetherTarget` |
| `DestoryAt` / `Delay` | 销毁延迟 / 延迟生效（毫秒） |
| `Name` | 名称，供 `RemoveDraw` 使用 |

需要逐帧变化时传回调，回调参数是本次绘制的 `DrawProperties`：

```cs
accessory.Method.SendDraw(DrawModeEnum.Default, DrawTypeEnum.Donut, prop, dp => dp.Radian += 0.01f);
```

更复杂的绘图写法可以看本仓库 `Karlin-Z/` 下的实战脚本，例如 [M1s.cs](./Karlin-Z/M1s.cs)。

## 生命周期与注意事项

- 载入脚本：实例化类 → 调用 `Init` → 开始分发事件；战斗重置、区域切换会再次调用 `Init`。
- 脚本被移除或重新加载时，若类实现了 `IDisposable`，会调用 `Dispose()`——请在这里释放 `Timer` 等资源。
- 一个 `.cs` 只放一个脚本类（提交到本库的硬性要求）。
