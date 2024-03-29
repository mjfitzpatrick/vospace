
package edu.caltech.vao.vospace;

import javax.ws.rs.core.Response;
import javax.ws.rs.ext.ExceptionMapper;
import javax.ws.rs.ext.Provider;

/**
 * Exception mapper for mapping a VOSpaceException that was thrown by the service
 */
@Provider
public class VOSpaceExceptionMapper implements ExceptionMapper<VOSpaceException> {

    public Response toResponse(VOSpaceException e) {
        // System.out.println(e.getStatusCode() + " " + e.getFault());
        // if (e.getIdentifier() != null) System.out.println(e.getIdentifier());
        return Response.status(e.getStatusCode()).entity(e.getMessage()).header(
                "X-VO-Fault",e.getFault()).header("X-VO-Identifier",e.getIdentifier()).build();
    }

}