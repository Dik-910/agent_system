## add_net

### JsonConfig
```Json
{
  "briefInfo": "add net",
  "description": "Creates nets in the current design or its subdesign",
  "isAtLeastOneArg": false,
}
```
### Argument

**-power**

```Json
{
  "token": ["-power"],
  "description": "[-power]  (Specifies the option of the power)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```
**-ground**

```Json
{
  "token": ["-ground"],
  "description": "[-ground]  (Specifies the option of the ground)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0,
  "debugOnly": true
}
```
**net_list**

```Json
{
  "token": [],
  "description": "[net_list]  (Specifies the names of the created nets)",
  "argType": "string",
  "isOptional": true,
  "argNumber": [0,0],
  "orderNum": 0
}
```


- Description:
  Specifies a list of nets to be created.
- Valid Value: string
- Default Value: N/A
- Rule: None

- Syntax
```
<*net_list*>
net_list := {<*net_name*>+}
net_name := <string>
```
### Description

Add_net command creates a new network in the current design. If add_net command succeeds, all its parameters must succeed. If any object does not meet the requirements, the netlist remains unchanged.

To bundle scalar nets into buses, use the add_bus command.

When you create nets with add_net, they are not connected. To establish this connection, use the connect_net command. To remove nets from the current design, use the delete_net command.

### Example

Scenario 1: The following example uses add_net to create net objects in the current design:

```
add_net {N1 N2 N3 N4}
get_obj_nets N*
```

Scenario 2: The following example uses add_net to create net objects in the mid subdesign.

```
add_net {H1/N1 H1/N2 H1/N3 H1/N4}
get_obj_nets */N*
```

### See Also

[delete_net](#delete_net)

[get_obj_nets](#get_obj_nets)
