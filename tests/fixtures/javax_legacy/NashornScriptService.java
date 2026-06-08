package com.example.legacy;

import javax.script.ScriptEngine;
import javax.script.ScriptEngineManager;
import jdk.nashorn.api.scripting.NashornScriptEngine;

public class NashornScriptService {

    public Object eval(String script) throws Exception {
        ScriptEngine engine = new ScriptEngineManager().getEngineByName("nashorn");
        return engine.eval(script);
    }
}
