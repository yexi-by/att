# 示例

输入：

```json
{
  "terminology": [
    {
      "source": "ミストリア",
      "translation": "米斯特里亚"
    }
  ],
  "groups": [
    {
      "kind": "event_dialogue",
      "units": [
        {
          "role": "speaker",
          "text": ["若い案内人"]
        },
        {
          "id": "0",
          "role": "body",
          "type": "free",
          "text": [
            "雨は、まだ止みそうにない。",
            "それでも夜明けまでには、",
            "ミストリアの峠を越えておきたいんだ。"
          ]
        },
        {
          "id": "1",
          "role": "body",
          "type": "free",
          "text": [
            "町に着いたらまず宿を確保しよう。⟦ATT_ACTOR_NAME_WHOLE_0000⟧の行方を探るのは、ひと息ついてからでも遅くないはずだ。"
          ]
        }
      ]
    },
    {
      "kind": "event_choices",
      "units": [
        {
          "id": "2",
          "role": "choices",
          "type": "strict",
          "text": ["急いで峠を越える", "", "村へ引き返す"]
        }
      ]
    }
  ]
}
```

输出：

```json
{
  "0": {
    "source": [
      "雨は、まだ止みそうにない。",
      "それでも夜明けまでには、",
      "ミストリアの峠を越えておきたいんだ。"
    ],
    "translation": [
      "这雨看样子一时半会儿还停不了。",
      "不过我还是想赶在天亮前翻过米斯特里亚山口。"
    ]
  },
  "1": {
    "source": [
      "町に着いたらまず宿を確保しよう。⟦ATT_ACTOR_NAME_WHOLE_0000⟧の行方を探るのは、ひと息ついてからでも遅くないはずだ。"
    ],
    "translation": [
      "到了镇上先找家旅店落脚吧。",
      "打听⟦ATT_ACTOR_NAME_WHOLE_0000⟧下落的事，等歇口气再说也不迟。"
    ]
  },
  "2": {
    "source": ["急いで峠を越える", "", "村へ引き返す"],
    "translation": ["趁早翻过山口", "", "折返村里"]
  }
}
```
