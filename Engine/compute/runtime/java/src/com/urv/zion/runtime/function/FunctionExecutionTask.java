package com.urv.zion.runtime.function;


import com.urv.zion.bus.BusDatagram;
import com.urv.zion.runtime.api.Api;
import com.urv.zion.runtime.context.Context;

import redis.clients.jedis.Jedis;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;

import org.json.simple.JSONObject;
import org.json.simple.parser.JSONParser;
import org.json.simple.parser.ParseException;
import org.slf4j.Logger;


public class FunctionExecutionTask implements Runnable {
	private Logger logger_;
	private Properties prop_;
	private Function function_;
	private BusDatagram dtg_;
	private FileOutputStream functionLog_;
	private Jedis redis_;
	
	private Context ctx;
	private Api api;
	
	private Map<String, String> object_metadata = null;
	private Map<String, String> request_headers = null;
	private Map<String, String> functionParameters = null;
	
	private FileDescriptor inputStreamFd = null;
	private FileDescriptor outputStreamFd = null;
	private FileDescriptor commandFd = null;

	/*------------------------------------------------------------------------
	 * CTOR
	 * */
	public FunctionExecutionTask(BusDatagram dtg, Properties prop, Jedis redis, Function function, FileOutputStream functionLog, Logger logger) {
		this.dtg_ = dtg;
		this.prop_ = prop;
		this.function_ = function;
		this.logger_ = logger;
		this.functionLog_ = functionLog;
		this.redis_ = redis;
		
		
		logger_.trace("Function execution task created");	
	}
	
	
	/*------------------------------------------------------------------------
	 * processDatagram
	 * 
	 * Process input datagram
	 * */
	@SuppressWarnings("unchecked")
	private void processDatagram(){
		HashMap<String, String>[] data = this.dtg_.getFilesMetadata();
		JSONObject metadata;
		
		outputStreamFd = this.dtg_.getFiles()[0];
		logger_.trace("Got object output stream");

		commandFd = this.dtg_.getFiles()[1];
		logger_.trace("Got Function command stream");
			
		inputStreamFd = this.dtg_.getFiles()[2];
		try {
			metadata = (JSONObject)new JSONParser().parse(data[2].get("data"));
			object_metadata = (Map<String, String>) metadata.get("object_metadata");
			request_headers = (Map<String, String>) metadata.get("request_headers");
			functionParameters = (Map<String, String>) metadata.get("parameters");
		} catch (ParseException e) {
			logger_.trace("Error parsing object headers, request metadata and parameters");
		}
		metadata = null;
		logger_.trace("Got object input stream, request headers, object metadata and function parameters");
		
		this.api = new Api(redis_, prop_, request_headers, logger_);
		this.ctx = new Context(inputStreamFd, outputStreamFd, functionParameters, functionLog_, commandFd, 
						  	   object_metadata, request_headers, logger_, api.swift);
	}

	
	/*------------------------------------------------------------------------
	 * run
	 * 
	 * Actual function invocation
	 * */
	public void run() {
		
		processDatagram();
		
		IFunction function = this.function_.getFunction();
		String functionName = this.function_.getName();
		
		logger_.trace("START: Going to execute '"+functionName+"' function");
		function.invoke(this.ctx, this.api);
		ctx.close();
		api.close();
		logger_.trace("END: Function '"+functionName+"' executed");

	}
}