## delete_net

### JsonConfig
```Json
{
  "briefInfo": "delete net",
  "description": "Delete nets from the current design",
  "isAtLeastOneArg": false,
}
```
### Argument

**net_list**

```Json
{
  "token": [],
  "description": "[net_list]  (Specifies a list of nets to remove from the current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the list of nets to remove from the current design. The -all and net_list are mutually exclusive and cannot be specified at the same time.
- Valid Value: list.string
- Default Value: N/A
- Rule: '-all' and 'net_list' are exclusive.

- Syntax
```
<*net_list*>
net_list := <list.string>
```
**-all**

```Json
{
  "token": ["-all"],
  "description": "[-all]  (Removes all nets in the current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Indicates that all nets will be removed from the current design. The -all and net_list are mutually exclusive and cannot be specified at the same time.
- Valid Value: N/A
- Default Value: N/A
- Rule: '-all' and 'net_list' are exclusive.

- Syntax
```
[-all]
```
**-only_physical**

```Json
{
  "token": ["-only_physical"],
  "description": "[-only_physical]  (Removes only physical nets in the current design)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```
### Description

The delete_net command removes specified nets or all nets from the current design. As with all other netlist editing commands, for the delete_net command to succeed, all of its arguments must succeed. If any argument fails, the netlist remains unchanged. The delete_net command returns 1 on success and 0 on failure. Each specified net must be in range, that is, on or below the current instance.

### Example

Scenario : In the following example, nets are removed if their names begin with G.

```
delete_net [get_obj_nets G*]
```

### See Also

[add_net](#add_net)

[get_obj_nets](#get_obj_nets)
