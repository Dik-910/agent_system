## disconnect_net

### JsonConfig
```Json
{
  "briefInfo": "disconnect net",
  "description": "Disconnects a net from specified pins or ports, or from all pins and ports",
  "isAtLeastOneArg": true,
}
```
### Argument

**net**

```Json
{
  "token": [],
  "description": "[net]  (The name of the net to be disconnected)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the name of the net to be disconnected.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
<*net*>
net := <list.string>
```
**object_list**

```Json
{
  "token": [],
  "description": "[object_list]  (A list of pins or ports to be disconnected from net)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies a list of pins or ports to be disconnected from net.
- Valid Value: list.string
- Default Value: N/A
- Rule: '-all' and 'object_list' are exclusive.

- Syntax
```
<*object_list*>
object_list := <list.string>
```
**-all**

```Json
{
  "token": ["-all"],
  "description": "[-all]  (Indicates that all pins and ports are to be disconnected from net)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specify this option to disconnect all connected pins and ports objects from net.
- Valid Value: N/A
- Default Value: N/A
- Rule: '-all' and 'object_list' are exclusive.

- Syntax
```
[-all]
```
### Description

The disconnect_net command disconnects pins and ports from a net in the current design. All objects meet the connection requirements for the disconnect_net command to execute successfully. If any object validation fails, the netlist remains unchanged. If you specify a net without any connection with -all option, this command will do nothing but silently return success.

You cannot disconnect an unconnected pin or a port.

You cannot disconnect a pin or a port that is not connected to net.

You cannot disconnect a hierarchical pin or a port that has the same name as net.

### Example

Scenario : The following example shows the basic usage of this directive.

```
get_obj_connected [get_obj_nets net_3]
disconnect_net net_3 inst_1/QN
get_obj_connected [get_obj_nets net_3]
get_obj_connected [get_obj_nets G9]
disconnect_net G9 -all
```

### See Also

[connect_net](#connect_net)

[get_obj_nets](#get_obj_nets)
