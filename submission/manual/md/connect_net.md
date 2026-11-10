## connect_net

### JsonConfig
```Json
{
  "briefInfo": "connect net",
  "description": "Connects a net to specified pins or ports",
  "isAtLeastOneArg": true,
}
```
### Argument

**net**

```Json
{
  "token": [],
  "description": "[net]  (The net to connect)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the name of the net to which the pins or ports to connect.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*net*>
net := <string>
```
**object_list**

```Json
{
  "token": [],
  "description": "[object_list]  (A list of pins and ports to which the net is to be connected)",
  "argType": "string",
  "isOptional": false,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies the list of pins or ports connected to a net.
- Valid Value: list.string
- Default Value: N/A
- Rule: None

- Syntax
```
<*object_list*>
object_list := {<*object_name*>+}
object_name := <string>
```
### Description

Connect the pins and ports to the network in the current design. All objects meet the connection requirements for the connect_net command to execute successfully. If any object validation fails, the netlist remains unchanged.

The following specifies the requirements that need to be met by the objects to be connected:

The net and each pin and port specified in the object_spec must be in same hierarchy.

You cannot connect net to a pin that is already connected.

You cannot connect across a hierarchical boundary. For example, you cannot connect a port to a net at a hierarchical level other than the top of the design.

### Example

Scenario 1: The following example creates a cell and a net and then connects the new net to the output of the new cell:

```
add_inst new_cell gscl45nm/BUFX2
add_net new_net
connect_net [get_obj_nets new_net] [get_obj_pins new_cell/A]
```

Scenario 2: The following example shows the incorrect usage of connecting a pin that already has a connection:

```
connect_net net_1 {inst_0/Y inst_1/D}
```

### See Also

[add_net](#add_net)

[disconnect_net](#disconnect_net)

[delete_net](#delete_net)
